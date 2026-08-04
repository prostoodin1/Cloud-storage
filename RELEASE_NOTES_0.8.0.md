# Cloud Storage 0.8.0 — accounts and mobile

- Added persistent user passwords and login from multiple Desktop, Android and iPhone devices.
- Every new device remains pending until an administrator approves it; knowing the password alone does not grant file access.
- Added password creation/reset to Server Manager. A reset invalidates existing remote sessions.
- Kept one-time invitation codes as a first-use and recovery option.
- Added the shared Expo/React Native mobile client with files, folders, photo/document upload, downloads, offline copies and a resumable transfer queue.
- Added secure mobile token/session storage using the operating-system Keychain/Keystore.
- Added a narrow role-gated mobile server view for monitoring, device approval/revocation and confirmed read-only mode without exposing the Manager API.
- Added Android/iOS validation and Android preview APK generation in GitHub Actions.
- Expanded Core, LAN, zrok, client and security tests for the new account and mobile-admin flows.
