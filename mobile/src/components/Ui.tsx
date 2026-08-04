import type { PropsWithChildren, ReactNode } from 'react';
import {
  ActivityIndicator,
  Pressable,
  SafeAreaView,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  type TextInputProps,
  View,
} from 'react-native';

import { colors, radius, spacing } from '../theme';

export function Screen({
  children,
  scroll = true,
}: PropsWithChildren<{ scroll?: boolean }>) {
  return (
    <SafeAreaView style={styles.safe}>
      {scroll ? (
        <ScrollView contentContainerStyle={styles.screen} keyboardShouldPersistTaps="handled">
          {children}
        </ScrollView>
      ) : (
        <View style={styles.screen}>{children}</View>
      )}
    </SafeAreaView>
  );
}

export function Brand() {
  return (
    <View style={styles.brandRow}>
      <View style={styles.brandMark}>
        <Text style={styles.brandMarkText}>CS</Text>
      </View>
      <View>
        <Text style={styles.brand}>CLOUD STORAGE</Text>
        <Text style={styles.brandSubtitle}>личное облако</Text>
      </View>
    </View>
  );
}

export function Title({ children, subtitle }: PropsWithChildren<{ subtitle?: string }>) {
  return (
    <View style={styles.titleBlock}>
      <Text style={styles.title}>{children}</Text>
      {subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}
    </View>
  );
}

export function Card({ children }: PropsWithChildren) {
  return <View style={styles.card}>{children}</View>;
}

export function Field({ label, ...props }: TextInputProps & { label: string }) {
  return (
    <View style={styles.field}>
      <Text style={styles.fieldLabel}>{label}</Text>
      <TextInput
        {...props}
        autoCapitalize={props.autoCapitalize ?? 'none'}
        placeholderTextColor={colors.muted}
        selectionColor={colors.red}
        style={[styles.input, props.multiline && styles.multiline, props.style]}
      />
    </View>
  );
}

export function Button({
  title,
  onPress,
  secondary = false,
  danger = false,
  disabled = false,
  busy = false,
  icon,
}: {
  title: string;
  onPress: () => void;
  secondary?: boolean;
  danger?: boolean;
  disabled?: boolean;
  busy?: boolean;
  icon?: ReactNode;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled || busy}
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        secondary && styles.secondaryButton,
        danger && styles.dangerButton,
        (disabled || busy) && styles.disabled,
        pressed && styles.pressed,
      ]}
    >
      {busy ? <ActivityIndicator color={colors.white} /> : icon}
      <Text style={styles.buttonText}>{title}</Text>
    </Pressable>
  );
}

export function Notice({
  children,
  tone = 'blue',
}: PropsWithChildren<{ tone?: 'blue' | 'yellow' | 'red' | 'green' }>) {
  return (
    <View style={[styles.notice, styles[`${tone}Notice`]]}>
      <Text style={styles.noticeText}>{children}</Text>
    </View>
  );
}

export function Empty({ children }: PropsWithChildren) {
  return <Text style={styles.empty}>{children}</Text>;
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.background },
  screen: {
    flexGrow: 1,
    paddingHorizontal: spacing.md,
    paddingTop: spacing.lg,
    paddingBottom: 96,
    gap: spacing.md,
    backgroundColor: colors.background,
  },
  brandRow: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm, marginBottom: spacing.sm },
  brandMark: {
    width: 48,
    height: 48,
    borderRadius: 13,
    backgroundColor: colors.red,
    alignItems: 'center',
    justifyContent: 'center',
  },
  brandMarkText: { color: colors.white, fontSize: 18, fontWeight: '900' },
  brand: { color: colors.text, fontWeight: '800', letterSpacing: 1.4, fontSize: 17 },
  brandSubtitle: { color: colors.muted, fontSize: 12 },
  titleBlock: { gap: spacing.xs },
  title: { color: colors.text, fontWeight: '800', fontSize: 30 },
  subtitle: { color: colors.muted, fontSize: 15, lineHeight: 21 },
  card: {
    backgroundColor: colors.surface,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: radius.md,
    padding: spacing.md,
    gap: spacing.md,
  },
  field: { gap: spacing.xs },
  fieldLabel: { color: colors.text, fontSize: 13, fontWeight: '700' },
  input: {
    minHeight: 49,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surfaceAlt,
    color: colors.text,
    paddingHorizontal: 14,
    fontSize: 16,
  },
  multiline: { minHeight: 90, paddingTop: 12, textAlignVertical: 'top' },
  button: {
    minHeight: 49,
    borderRadius: radius.sm,
    paddingHorizontal: spacing.md,
    backgroundColor: colors.red,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.sm,
  },
  secondaryButton: { backgroundColor: colors.surfaceAlt, borderWidth: 1, borderColor: colors.border },
  dangerButton: { backgroundColor: colors.redDark },
  buttonText: { color: colors.white, fontWeight: '800', fontSize: 15 },
  disabled: { opacity: 0.48 },
  pressed: { opacity: 0.78 },
  notice: { borderRadius: radius.md, borderLeftWidth: 4, padding: spacing.md },
  blueNotice: { backgroundColor: '#131c27', borderLeftColor: colors.blue },
  yellowNotice: { backgroundColor: '#241c12', borderLeftColor: colors.yellow },
  redNotice: { backgroundColor: '#211518', borderLeftColor: colors.red },
  greenNotice: { backgroundColor: '#13221a', borderLeftColor: colors.green },
  noticeText: { color: colors.text, lineHeight: 21 },
  empty: {
    color: colors.muted,
    textAlign: 'center',
    paddingVertical: spacing.xl,
    lineHeight: 22,
  },
});
