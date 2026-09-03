<?php

namespace MediaWiki\Extension\ImgGuard;

use MediaWiki\Config\Config;
use MediaWiki\Logger\LoggerFactory;
use MediaWiki\Shell\Shell;
use Wikimedia\ObjectCache\BagOStuff;
use Wikimedia\ObjectCache\WANObjectCache;

class Classifier {

	private const EXIT_MEANING = [
		1 => 'failure',
		2 => 'budget-exceeded',
		3 => 'unsupported-format',
	];

	public function __construct(
		private readonly Config $config,
		private readonly WANObjectCache $cache,
		private readonly BagOStuff $stash
	) {
	}

	public function getVerdict( string $cacheKey, string $path ): ?array {
		$key = $this->cache->makeGlobalKey( 'imgguard-verdict', $cacheKey );

		return $this->cache->getWithSetCallback(
			$key,
			$this->config->get( 'ImgGuardCacheTtl' ),
			function ( $oldValue, &$ttl ) use ( $path ) {
				$slot = $this->acquireSlot();
				if ( $slot === null ) {
					$ttl = WANObjectCache::TTL_UNCACHEABLE;
					return [ 'error' => 'busy', 'flags' => [] ];
				}

				$verdict = null;
				try {
					$verdict = $this->runScript( $path );
				} finally {
					$this->releaseSlot( $slot );
				}

				if ( $verdict === null || !isset( $verdict['sfw'] ) ) {
					$ttl = $this->config->get( 'ImgGuardFailureCacheTtl' );
				}
				return $verdict;
			}
		);
	}

	/**
	 * @return string|null Slot key to release, '' when unlimited, null when none came free.
	 */
	private function acquireSlot(): ?string {
		$max = (int)$this->config->get( 'ImgGuardMaxConcurrent' );
		if ( $max <= 0 ) {
			return '';
		}

		$hold = (int)$this->config->get( 'ImgGuardTimeout' ) + 30;
		$deadline = microtime( true ) + 5.0;

		do {
			for ( $i = 0; $i < $max; $i++ ) {
				$key = $this->stash->makeGlobalKey( 'imgguard-slot', $i );
				if ( $this->stash->add( $key, 1, $hold ) ) {
					return $key;
				}
			}
			usleep( 250000 );
		} while ( microtime( true ) < $deadline );

		LoggerFactory::getInstance( 'ImgGuard' )->warning(
			'all {max} classification slots busy, refusing upload', [ 'max' => $max ]
		);
		return null;
	}

	private function releaseSlot( string $slot ): void {
		if ( $slot !== '' ) {
			$this->stash->delete( $slot );
		}
	}

	private function runScript( string $path ): ?array {
		$logger = LoggerFactory::getInstance( 'ImgGuard' );
		$timeout = $this->config->get( 'ImgGuardTimeout' );

		$budgetSeconds = max( 10, min( $timeout * 0.5, $timeout - 15 ) );

		try {
			$args = [
				'python3',
				$this->config->get( 'ImgGuardScriptPath' ),
				'--budget-seconds', (string)$budgetSeconds,
			];

			$matureModel = $this->config->get( 'ImgGuardMatureModelPath' );
			if ( is_string( $matureModel ) && $matureModel !== '' ) {
				$args[] = '--mature-model';
				$args[] = $matureModel;
			}

			$args[] = $this->config->get( 'ImgGuardModelPath' );
			$args[] = $path;

			$result = Shell::command( $args )
				->limits( [
					'time' => $timeout,
					'walltime' => $timeout,
					'memory' => $this->config->get( 'ImgGuardMemoryLimit' ),
				] )
				->execute();
		} catch ( \Exception $e ) {
			$logger->warning( 'classify.py could not be run: {message}', [
				'message' => $e->getMessage(),
			] );
			return null;
		}

		$exitCode = $result->getExitCode();
		if ( $exitCode !== 0 ) {
			$logger->warning( 'classify.py failed (exit {code}, {meaning}): {stderr}', [
				'code' => $exitCode,
				'meaning' => self::EXIT_MEANING[$exitCode] ?? 'unknown',
				'stderr' => $result->getStderr(),
			] );
			$detail = json_decode( $result->getStderr(), true );
			if ( is_array( $detail ) && isset( $detail['error'] ) ) {
				return [
					'error' => $detail['error'],
					'flags' => is_array( $detail['flags'] ?? null ) ? $detail['flags'] : [],
				];
			}
			return null;
		}

		$verdict = json_decode( $result->getStdout(), true );
		if ( !is_array( $verdict ) || !isset( $verdict['sfw'], $verdict['nsfw'], $verdict['nsfl'] ) ) {
			$logger->warning( 'classify.py returned unexpected output: {stdout}', [
				'stdout' => $result->getStdout(),
			] );
			return null;
		}

		return $verdict;
	}
}
