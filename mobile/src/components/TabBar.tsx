import { Pressable, StyleSheet, Text, View } from 'react-native';

import { colors, spacing } from '../theme';

export type MainTab = 'files' | 'transfers' | 'server' | 'settings';

const tabs: { id: MainTab; icon: string; label: string }[] = [
  { id: 'files', icon: '▤', label: 'Файлы' },
  { id: 'transfers', icon: '⇅', label: 'Передачи' },
  { id: 'server', icon: '▣', label: 'Сервер' },
  { id: 'settings', icon: '⚙', label: 'Настройки' },
];

export function TabBar({
  tab,
  onChange,
  badge,
  admin,
}: {
  tab: MainTab;
  onChange: (tab: MainTab) => void;
  badge: number;
  admin: boolean;
}) {
  const visibleTabs = admin ? tabs : tabs.filter((item) => item.id !== 'server');
  return (
    <View style={styles.bar}>
      {visibleTabs.map((item) => (
        <Pressable key={item.id} onPress={() => onChange(item.id)} style={styles.item}>
          <Text style={[styles.icon, tab === item.id && styles.active]}>{item.icon}</Text>
          <Text style={[styles.label, tab === item.id && styles.active]}>{item.label}</Text>
          {item.id === 'transfers' && badge > 0 ? (
            <View style={styles.badge}><Text style={styles.badgeText}>{badge}</Text></View>
          ) : null}
        </Pressable>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  bar: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
    minHeight: 76,
    paddingBottom: spacing.sm,
    flexDirection: 'row',
    backgroundColor: '#111317f5',
    borderTopWidth: 1,
    borderTopColor: colors.border,
  },
  item: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 2 },
  icon: { color: colors.muted, fontSize: 22, fontWeight: '800' },
  label: { color: colors.muted, fontSize: 11, fontWeight: '700' },
  active: { color: colors.red },
  badge: {
    position: 'absolute',
    top: 8,
    right: '28%',
    minWidth: 18,
    height: 18,
    paddingHorizontal: 4,
    borderRadius: 9,
    backgroundColor: colors.red,
    alignItems: 'center',
    justifyContent: 'center',
  },
  badgeText: { color: colors.white, fontSize: 10, fontWeight: '900' },
});
