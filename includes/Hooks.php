<?php

namespace MediaWiki\Extension\ImgGuard;

use ManualLogEntry;
use MediaWiki\Block\BlockUserFactory;
use MediaWiki\Config\Config;
use MediaWiki\Title\Title;
use MediaWiki\Upload\Hook\UploadStashFileHook;
use MediaWiki\Upload\Hook\UploadVerifyUploadHook;
use MediaWiki\Upload\UploadBase;
use MediaWiki\User\ActorNormalization;
use MediaWiki\User\User;
use Wikimedia\Mime\MimeAnalyzer;
use Wikimedia\Rdbms\IConnectionProvider;

class Hooks implements UploadVerifyUploadHook, UploadStashFileHook {

	public function __construct(
		private readonly Config $config,
		private readonly Classifier $classifier,
		private readonly MimeAnalyzer $mimeAnalyzer,
		private readonly BlockUserFactory $blockUserFactory,
		private readonly ActorNormalization $actorNormalization,
		private readonly IConnectionProvider $dbProvider
	) {
	}

	public function onUploadVerifyUpload( UploadBase $upload, User $user, ?array $props, $comment, $pageText, &$error ) {
		return $this->check( $upload, $user, $props ?? [], $error );
	}

	public function onUploadStashFile( UploadBase $upload, User $user, ?array $props, &$error ) {
		return $this->check( $upload, $user, $props ?? [], $error );
	}

	private function check( UploadBase $upload, User $user, array $props, &$error ): bool {
		if ( $user->isAllowed( 'imgguard-bypass' ) ) {
			return true;
		}

		$title = $upload->getTitle();

		$path = $upload->getTempPath();
		if ( $path === null || $path === '' || !is_file( $path ) ) {
			return true;
		}

		$mime = $props['mime'] ?? null;
		if ( !is_string( $mime ) || $mime === '' ) {
			$mime = $this->mimeAnalyzer->guessMimeType( $path, false );
		}
		if ( $mime === '' ) {
			return $this->abstain( 'no-mime', $user, $title, $error );
		}
		if (
			!self::isEligibleMime( $mime, $this->config->get( 'ImgGuardEligibleMimeTypes' ) ) &&
			!self::looksLikeContainer( $path )
		) {
			return true;
		}

		$cacheKey = hash_file( 'sha256', $path );
		if ( $cacheKey === false ) {
			return $this->abstain( 'hash-failed', $user, $title, $error );
		}

		$verdict = $this->classifier->getVerdict( $cacheKey, $path );

		if ( $verdict === null || !isset( $verdict['sfw'] ) ) {
			$reason = $verdict['error'] ?? 'error';
			$this->log( 'fail', $user, $title, [], $reason );
			if ( $reason === 'busy' ) {
				$error = [ 'imgguard-busy' ];
				return false;
			}
			if ( $this->config->get( 'ImgGuardFailClosed' ) ) {
				$error = [ 'imgguard-rejected' ];
				return false;
			}
			return true;
		}

		$threshold = $this->config->get( 'ImgGuardSfwThreshold' );
		$flagged = $verdict['sfw'] < $threshold;
		$enforce = $this->config->get( 'ImgGuardEnforce' );
		$borderline = !$flagged &&
			( $verdict['sfw'] - $threshold ) < $this->config->get( 'ImgGuardBorderlineMargin' );

		$subtype = $flagged ? ( $enforce ? 'reject' : 'monitor' ) : ( $borderline ? 'borderline' : 'pass' );
		$this->log( $subtype, $user, $title, $verdict );

		if ( $subtype === 'reject' ) {
			$this->maybeAutoBlock( $user );
		}

		if ( $flagged && $enforce ) {
			$error = [ 'imgguard-rejected' ];
			return false;
		}

		return true;
	}

	private function abstain( string $reason, User $user, ?Title $title, &$error ): bool {
		$this->log( 'fail', $user, $title, [], $reason );
		if ( $this->config->get( 'ImgGuardFailClosed' ) ) {
			$error = [ 'imgguard-rejected' ];
			return false;
		}
		return true;
	}

	public static function looksLikeContainer( string $path ): bool {
		$handle = @fopen( $path, 'rb' );
		if ( $handle === false ) {
			return false;
		}
		$magic = fread( $handle, 8 );
		fclose( $handle );
		if ( !is_string( $magic ) || strlen( $magic ) < 5 ) {
			return false;
		}
		return str_starts_with( $magic, "PK\x03\x04" )
			|| str_starts_with( $magic, '%PDF-' )
			|| str_starts_with( $magic, 'OggS' )
			|| str_starts_with( $magic, "\xd0\xcf\x11\xe0" );
	}

	public static function isEligibleMime( string $mime, array $extraTypes ): bool {
		if ( in_array( $mime, $extraTypes, true ) ) {
			return true;
		}
		$type = strstr( $mime, '/', true );
		return $type === 'image' || $type === 'video';
	}

	private function log(
		string $subtype, User $user, ?Title $title, array $verdict, string $reason = ''
	): void {
		if ( $subtype === 'pass' && !$this->config->get( 'ImgGuardLogPasses' ) ) {
			return;
		}
		if ( $title === null ) {
			return;
		}

		$logEntry = new ManualLogEntry( 'imgguard', $subtype );
		$logEntry->setPerformer( $user );
		$logEntry->setTarget( $title );
		$logEntry->setParameters( [
			'6::sfw' => isset( $verdict['sfw'] ) ? round( $verdict['sfw'], 3 ) : null,
			'15::reason' => $reason,
		] );
		$logEntry->insert();
	}

	private function maybeAutoBlock( User $user ): void {
		if ( !$this->config->get( 'ImgGuardAutoBlockEnabled' ) ) {
			return;
		}

		if ( $user->isAllowed( 'imgguard-autoblock-exempt' ) ) {
			return;
		}

		if ( $user->getBlock() !== null ) {
			return;
		}

		$count = $this->countRejections( $user );
		if ( $count < $this->config->get( 'ImgGuardAutoBlockThreshold' ) ) {
			return;
		}

		$blocker = User::newSystemUser( 'ImgGuard', [ 'steal' => true ] );
		if ( $blocker === null ) {
			return;
		}

		$duration = $this->config->get( 'ImgGuardAutoBlockDuration' );
		$expiry = wfTimestamp( TS_MW, strtotime( $duration ) );
		if ( $expiry === false ) {
			return;
		}

		$blockUser = $this->blockUserFactory->newBlockUser(
			$user,
			$blocker,
			$expiry,
			wfMessage( 'imgguard-autoblock-reason' )->inContentLanguage()->text(),
			[
				'isHardBlock' => true,
				'isAutoblocking' => false,
				'isCreateAccountBlocked' => false,
				'isEmailBlocked' => false,
				'isUserTalkEditBlocked' => false,
			]
		);
		$status = $blockUser->placeBlockUnsafe();
		if ( !$status->isGood() ) {
			return;
		}

		$logEntry = new ManualLogEntry( 'imgguard', 'autoblock' );
		$logEntry->setPerformer( $blocker );
		$logEntry->setTarget( $user->getUserPage() );
		$logEntry->setParameters( [
			'4::duration' => $duration,
			'5::count' => $count,
		] );
		$logEntry->insert();
	}

	private function countRejections( User $user ): int {
		$dbr = $this->dbProvider->getReplicaDatabase();
		$actorId = $this->actorNormalization->findActorId( $user, $dbr );
		if ( $actorId === null ) {
			return 0;
		}

		$window = $this->config->get( 'ImgGuardAutoBlockWindow' );
		$since = wfTimestamp( TS_MW, strtotime( "-{$window}" ) );

		return (int)$dbr->newSelectQueryBuilder()
			->select( 'COUNT(*)' )
			->from( 'logging' )
			->where( [
				'log_type' => 'imgguard',
				'log_action' => 'reject',
				'log_actor' => $actorId,
				$dbr->expr( 'log_timestamp', '>=', $dbr->timestamp( $since ) ),
			] )
			->caller( __METHOD__ )
			->fetchField();
	}
}
