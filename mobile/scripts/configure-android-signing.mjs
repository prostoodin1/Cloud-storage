import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const buildFile = resolve('android/app/build.gradle');
let source = readFileSync(buildFile, 'utf8');
const required = [
  'CLOUD_STORAGE_ANDROID_KEYSTORE',
  'CLOUD_STORAGE_ANDROID_KEYSTORE_PASSWORD',
  'CLOUD_STORAGE_ANDROID_KEY_ALIAS',
  'CLOUD_STORAGE_ANDROID_KEY_PASSWORD',
];
for (const name of required) {
  if (!process.env[name]) throw new Error(`${name} is required for a release build`);
}

const debugConfig = `    signingConfigs {
        debug {
            storeFile file('debug.keystore')
            storePassword 'android'
            keyAlias 'androiddebugkey'
            keyPassword 'android'
        }
    }`;
if (!source.includes(debugConfig)) throw new Error('Expo Android signing block was not found');

const releaseConfig = `${debugConfig.slice(0, -6)}        release {
            storeFile file(System.getenv('CLOUD_STORAGE_ANDROID_KEYSTORE'))
            storePassword System.getenv('CLOUD_STORAGE_ANDROID_KEYSTORE_PASSWORD')
            keyAlias System.getenv('CLOUD_STORAGE_ANDROID_KEY_ALIAS')
            keyPassword System.getenv('CLOUD_STORAGE_ANDROID_KEY_PASSWORD')
        }
    }`;
source = source.replace(debugConfig, releaseConfig);
source = source.replace(
  /release \{([\s\S]*?)signingConfig signingConfigs\.debug/,
  'release {$1signingConfig signingConfigs.release',
);
writeFileSync(buildFile, source, 'utf8');
