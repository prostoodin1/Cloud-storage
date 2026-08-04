import { useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, View } from 'react-native';
import * as DocumentPicker from 'expo-document-picker';
import * as ImagePicker from 'expo-image-picker';

import { Button, Card, Empty, Field, Notice, Screen, Title } from '../components/Ui';
import { colors, radius, spacing } from '../theme';
import type { FileEntry, SpaceRecord } from '../types';

export interface UploadAsset {
  uri: string;
  name: string;
  size: number;
  mimeType: string;
}

function joinPath(directory: string, name: string): string {
  return [directory, name].filter(Boolean).join('/');
}

function formatBytes(value = 0): string {
  if (value < 1024) return `${value} Б`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} КБ`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} МБ`;
  return `${(value / 1024 ** 3).toFixed(1)} ГБ`;
}

export function FilesScreen({
  spaces,
  selectedSpaceId,
  directory,
  entries,
  loading,
  error,
  onSelectSpace,
  onOpenDirectory,
  onRefresh,
  onUpload,
  onDownload,
  onDelete,
  onCreateDirectory,
}: {
  spaces: SpaceRecord[];
  selectedSpaceId: string;
  directory: string;
  entries: FileEntry[];
  loading: boolean;
  error: string;
  onSelectSpace: (id: string) => void;
  onOpenDirectory: (path: string) => void;
  onRefresh: () => void;
  onUpload: (asset: UploadAsset) => void;
  onDownload: (entry: FileEntry) => void;
  onDelete: (entry: FileEntry) => void;
  onCreateDirectory: (name: string) => void;
}) {
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [folderName, setFolderName] = useState('');

  const pickDocument = async () => {
    const result = await DocumentPicker.getDocumentAsync({
      copyToCacheDirectory: true,
      multiple: false,
      type: '*/*',
    });
    const asset = result.assets?.[0];
    if (!result.canceled && asset) {
      onUpload({
        uri: asset.uri,
        name: asset.name,
        size: asset.size ?? 0,
        mimeType: asset.mimeType ?? 'application/octet-stream',
      });
    }
  };

  const pickPhoto = async () => {
    const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!permission.granted) {
      Alert.alert('Нет доступа', 'Разрешите выбор фотографий в настройках телефона.');
      return;
    }
    const result = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ['images', 'videos'],
      quality: 1,
    });
    const asset = result.assets?.[0];
    if (!result.canceled && asset) {
      onUpload({
        uri: asset.uri,
        name: asset.fileName ?? `Фото-${Date.now()}.jpg`,
        size: asset.fileSize ?? 0,
        mimeType: asset.mimeType ?? 'image/jpeg',
      });
    }
  };

  const createFolder = () => {
    const name = folderName.trim().replace(/[\\/]/g, '');
    if (!name) return;
    onCreateDirectory(name);
    setFolderName('');
    setCreatingFolder(false);
  };

  const parent = directory.split('/').slice(0, -1).join('/');

  return (
    <Screen>
      <Title subtitle={directory ? `/${directory}` : 'Личное и доступные общие пространства'}>Мои файлы</Title>
      <View style={styles.spaces}>
        {spaces.map((space) => (
          <Pressable
            key={space.id}
            onPress={() => onSelectSpace(space.id)}
            style={[styles.spacePill, selectedSpaceId === space.id && styles.spacePillActive]}
          >
            <Text style={[styles.spaceText, selectedSpaceId === space.id && styles.spaceTextActive]}>
              {space.name}
            </Text>
          </Pressable>
        ))}
      </View>
      <View style={styles.actions}>
        <Button title="Загрузить файл" onPress={pickDocument} />
        <Button title="Фото / видео" onPress={pickPhoto} secondary />
        <Button title="Новая папка" onPress={() => setCreatingFolder((value) => !value)} secondary />
      </View>
      {creatingFolder ? (
        <Card>
          <Field
            label="Название папки"
            value={folderName}
            onChangeText={setFolderName}
            placeholder="Документы"
            autoCapitalize="sentences"
          />
          <Button title="Создать папку" onPress={createFolder} />
        </Card>
      ) : null}
      {directory ? (
        <Pressable onPress={() => onOpenDirectory(parent)} style={styles.upRow}>
          <Text style={styles.folderIcon}>←</Text>
          <Text style={styles.entryName}>Назад</Text>
        </Pressable>
      ) : null}
      {error ? <Notice tone="red">{error}</Notice> : null}
      {loading ? <Empty>Обновляем список файлов…</Empty> : null}
      {!loading && entries.length === 0 ? (
        <Empty>Здесь пока пусто. Загрузите файл, фотографию или создайте папку.</Empty>
      ) : null}
      {entries.map((entry) => {
        const logicalPath = joinPath(directory, entry.name);
        return (
          <Card key={`${entry.type}:${entry.name}`}>
            <Pressable
              disabled={entry.type !== 'directory'}
              onPress={() => onOpenDirectory(logicalPath)}
              style={styles.entryMain}
            >
              <Text style={styles.entryIcon}>{entry.type === 'directory' ? '▰' : '▤'}</Text>
              <View style={styles.entryText}>
                <Text style={styles.entryName} numberOfLines={2}>{entry.name}</Text>
                <Text style={styles.entryMeta}>
                  {entry.type === 'directory'
                    ? 'Папка'
                    : `${formatBytes(entry.size_bytes)} · версия ${entry.version ?? 1}`}
                </Text>
              </View>
            </Pressable>
            <View style={styles.entryActions}>
              {entry.type === 'file' ? (
                <Pressable onPress={() => onDownload(entry)} style={styles.smallButton}>
                  <Text style={styles.smallButtonText}>Скачать</Text>
                </Pressable>
              ) : null}
              <Pressable
                onPress={() =>
                  Alert.alert(
                    `Удалить «${entry.name}»?`,
                    entry.type === 'file'
                      ? 'Файл будет перемещён в серверную корзину.'
                      : 'Папка удалится только если она пуста.',
                    [
                      { text: 'Отмена', style: 'cancel' },
                      { text: 'Удалить', style: 'destructive', onPress: () => onDelete(entry) },
                    ],
                  )
                }
                style={[styles.smallButton, styles.deleteButton]}
              >
                <Text style={styles.smallButtonText}>Удалить</Text>
              </Pressable>
            </View>
          </Card>
        );
      })}
      <Button title="Обновить" onPress={onRefresh} secondary busy={loading} />
    </Screen>
  );
}

const styles = StyleSheet.create({
  spaces: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  spacePill: {
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surface,
    borderRadius: 999,
    paddingVertical: 9,
    paddingHorizontal: 14,
  },
  spacePillActive: { backgroundColor: colors.red, borderColor: colors.red },
  spaceText: { color: colors.muted, fontWeight: '700' },
  spaceTextActive: { color: colors.white },
  actions: { gap: spacing.sm },
  upRow: {
    minHeight: 50,
    borderRadius: radius.md,
    backgroundColor: colors.surface,
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: spacing.md,
    gap: spacing.md,
  },
  folderIcon: { color: colors.blue, fontSize: 25, fontWeight: '800' },
  entryMain: { flexDirection: 'row', alignItems: 'center', gap: spacing.md },
  entryIcon: { color: colors.red, fontSize: 28, width: 34, textAlign: 'center' },
  entryText: { flex: 1, gap: 3 },
  entryName: { color: colors.text, fontSize: 16, fontWeight: '700' },
  entryMeta: { color: colors.muted, fontSize: 13 },
  entryActions: { flexDirection: 'row', justifyContent: 'flex-end', gap: spacing.sm },
  smallButton: {
    borderRadius: radius.sm,
    backgroundColor: colors.surfaceAlt,
    borderColor: colors.border,
    borderWidth: 1,
    paddingVertical: 9,
    paddingHorizontal: 13,
  },
  deleteButton: { borderColor: colors.redDark },
  smallButtonText: { color: colors.text, fontWeight: '700' },
});
