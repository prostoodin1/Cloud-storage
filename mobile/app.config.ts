import type { ConfigContext, ExpoConfig } from 'expo/config';

export default ({ config }: ConfigContext): ExpoConfig => {
  const isManager = process.env.EXPO_PUBLIC_APP_VARIANT === 'manager';

  return {
    ...config,
    name: isManager ? 'Cloud Storage Manager' : 'Cloud Storage',
    slug: isManager ? 'cloud-storage-mobile-manager' : 'cloud-storage-mobile',
    scheme: isManager ? 'cloudstoragemanager' : 'cloudstorage',
    ios: {
      ...config.ios,
      bundleIdentifier: isManager ? 'com.cloudstorage.manager' : 'com.cloudstorage.mobile',
    },
    android: {
      ...config.android,
      package: isManager ? 'com.cloudstorage.manager' : 'com.cloudstorage.mobile',
    },
  };
};
