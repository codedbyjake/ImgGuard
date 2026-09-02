<?php

namespace MediaWiki\Extension\ImgGuard\Maintenance;

use MediaWiki\Extension\ImgGuard\Hooks;
use MediaWiki\Maintenance\Maintenance;

// @codeCoverageIgnoreStart
$IP = getenv( 'MW_INSTALL_PATH' );
if ( $IP === false ) {
	$IP = __DIR__ . '/../../..';
}
require_once "$IP/maintenance/Maintenance.php";
// @codeCoverageIgnoreEnd

class RescanUploads extends Maintenance {

	public function __construct() {
		parent::__construct();
		$this->addDescription(
			'Re-run ImgGuard classification against existing uploaded files eligible for screening '
			. '(same MIME check the upload hook uses) and report the results, without logging or '
			. 'blocking. Useful after retuning ImgGuardSfwThreshold.'
		);
		$this->addOption( 'limit', 'Maximum number of files to check (default: all)', false, true );
		$this->addOption( 'start', 'Resume from this file name (exclusive), ordered by img_name', false, true );
		$this->setBatchSize( 200 );
		$this->requireExtension( 'ImgGuard' );
	}

	public function execute() {
		$services = $this->getServiceContainer();
		$classifier = $services->getService( 'ImgGuardClassifier' );
		$repo = $services->getRepoGroup()->getLocalRepo();
		$config = $services->getMainConfig();
		$threshold = $config->get( 'ImgGuardSfwThreshold' );
		$extraTypes = $config->get( 'ImgGuardEligibleMimeTypes' );

		$limit = (int)$this->getOption( 'limit', 0 );
		$lastName = $this->getOption( 'start', '' );

		$dbr = $this->getDB( DB_REPLICA );
		$batchSize = $this->getBatchSize();

		$checked = 0;
		$flagged = 0;
		$skipped = 0;
		$failed = 0;

		while ( true ) {
			$queryBuilder = $dbr->newSelectQueryBuilder()
				->select( [ 'img_name', 'img_major_mime', 'img_minor_mime' ] )
				->from( 'image' )
				->where( $lastName !== '' ? $dbr->expr( 'img_name', '>', $lastName ) : [] )
				->orderBy( 'img_name' )
				->limit( $batchSize )
				->caller( __METHOD__ );
			$rows = iterator_to_array( $queryBuilder->fetchResultSet() );
			if ( !$rows ) {
				break;
			}

			foreach ( $rows as $row ) {
				if ( $limit > 0 && $checked >= $limit ) {
					$this->reportSummary( $checked, $flagged, $skipped, $failed, $lastName );
					return;
				}

				$lastName = $row->img_name;
				$mime = "{$row->img_major_mime}/{$row->img_minor_mime}";

				$file = $repo->newFile( $row->img_name );
				if ( $file === null || !$file->exists() ) {
					continue;
				}
				$path = $file->getLocalRefPath();
				if ( $path === false ) {
					continue;
				}

				if ( !Hooks::isEligibleMime( $mime, $extraTypes ) && !Hooks::looksLikeContainer( $path ) ) {
					$skipped++;
					continue;
				}
				$cacheKey = hash_file( 'sha256', $path );
				if ( $cacheKey === false ) {
					continue;
				}

				$verdict = $classifier->getVerdict( $cacheKey, $path );
				$checked++;

				if ( $verdict === null || !isset( $verdict['sfw'] ) ) {
					$failed++;
					$this->output( "FAIL  {$row->img_name} ({$mime})\n" );
					continue;
				}

				if ( $verdict['sfw'] < $threshold ) {
					$flagged++;
					$this->output( sprintf( "FLAG  %s (sfw=%.3f)\n", $row->img_name, $verdict['sfw'] ) );
				}
			}
		}

		$this->reportSummary( $checked, $flagged, $skipped, $failed, $lastName );
	}

	private function reportSummary( int $checked, int $flagged, int $skipped, int $failed, string $lastName ): void {
		$this->output(
			"Checked {$checked} eligible files ({$flagged} flagged, {$failed} failed to classify), "
			. "{$skipped} ineligible files skipped. Last file seen: {$lastName}\n"
		);
	}
}

// @codeCoverageIgnoreStart
$maintClass = RescanUploads::class;
require_once RUN_MAINTENANCE_IF_MAIN;
// @codeCoverageIgnoreEnd
