module.exports = ({ config }) => {
  const manager = process.env.EXPO_PUBLIC_APP_VARIANT === 'manager';
  return {
    ...config,
    name: manager ? 'Cloud Storage Manager' : 'Cloud Storage',
    slug: manager ? 'cloud-storage-mobile-manager' : 'cloud-storage-mobile',
    scheme: manager ? 'cloudstorage-manager' : 'cloudstorage',
    ios: {
      ...config.ios,
      bundleIdentifier: manager ? 'com.cloudstorage.manager' : 'com.cloudstorage.mobile',
    },
    android: {
      ...config.android,
      package: manager ? 'com.cloudstorage.manager' : 'com.cloudstorage.mobile',
    },
  };
};
