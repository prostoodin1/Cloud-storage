import { useState } from 'react';

import { Brand, Button, Card, Field, Notice, Screen, Title } from '../components/Ui';
import type { ConnectionProfile } from '../types';

export function RemoteUnlockScreen({
  profile,
  onUnlock,
  onForget,
}: {
  profile: ConnectionProfile;
  onUnlock: (password: string) => Promise<void>;
  onForget: () => void;
}) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const unlock = async () => {
    if (password.length < 10) {
      setError('Введите пароль минимум из 10 символов.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await onUnlock(password);
      setPassword('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Интернет-вход не выполнен.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Screen>
      <Brand />
      <Title subtitle={`${profile.serverName} · @${profile.username}`}>Защитить интернет-сессию</Title>
      <Notice tone="green">Телефон подтверждён. Для доступа через zrok осталось повторно проверить пароль.</Notice>
      <Card>
        <Field
          label="Пароль аккаунта"
          value={password}
          onChangeText={setPassword}
          placeholder="Пароль не будет сохранён"
          secureTextEntry
          textContentType="password"
        />
        {error ? <Notice tone="red">{error}</Notice> : null}
        <Button title="Открыть мои файлы" onPress={unlock} busy={busy} />
        <Button title="Забыть подключение" onPress={onForget} secondary />
      </Card>
    </Screen>
  );
}
