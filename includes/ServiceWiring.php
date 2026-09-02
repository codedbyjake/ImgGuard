<?php

namespace MediaWiki\Extension\ImgGuard;

use MediaWiki\MediaWikiServices;

return [
	'ImgGuardClassifier' => static function ( MediaWikiServices $services ): Classifier {
		return new Classifier(
			$services->getMainConfig(),
			$services->getMainWANObjectCache(),
			$services->getMainObjectStash()
		);
	},
];
