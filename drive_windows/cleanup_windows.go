package main

import (
	"github.com/winfsp/go-winfsp"
	"unicode/utf16"
)

// Keep all of gofs' optional behaviours when overriding Cleanup. Close callbacks
// run asynchronously in WinFSP; Cleanup runs before the caller's CloseHandle returns.
type bridgeBehaviour interface {
	winfsp.BehaviourBase
	winfsp.BehaviourGetSecurityByName
	winfsp.BehaviourCreate
	winfsp.BehaviourOverwrite
	winfsp.BehaviourReadDirectory
	winfsp.BehaviourGetFileInfo
	winfsp.BehaviourGetSecurity
	winfsp.BehaviourGetVolumeInfo
	winfsp.BehaviourSetVolumeLabel
	winfsp.BehaviourSetBasicInfo
	winfsp.BehaviourSetFileSize
	winfsp.BehaviourRead
	winfsp.BehaviourWrite
	winfsp.BehaviourFlush
	winfsp.BehaviourCanDelete
	winfsp.BehaviourCleanup
	winfsp.BehaviourRename
	winfsp.BehaviourDefaultOptions
}

type synchronousBehaviour struct {
	bridgeBehaviour
	cloud *cloudFileSystem
}

func (b *synchronousBehaviour) GetVolumeInfo(_ *winfsp.FileSystemRef, info *winfsp.FSP_FSCTL_VOLUME_INFO) error {
	b.cloud.mu.Lock()
	value := b.cloud.volume
	b.cloud.mu.Unlock()
	total := max(int64(0), value.QuotaBytes)
	free := max(int64(0), total-max(int64(0), value.UsedBytes))
	info.TotalSize, info.FreeSize = uint64(total), uint64(free)
	name := utf16.Encode([]rune(value.Name))
	if len(name) > len(info.VolumeLabel) {
		name = name[:len(info.VolumeLabel)]
	}
	if len(name) > 0 && name[len(name)-1] >= 0xd800 && name[len(name)-1] <= 0xdbff {
		name = name[:len(name)-1]
	}
	clear(info.VolumeLabel[:])
	info.VolumeLabelLength = uint16(copy(info.VolumeLabel[:], name) * 2)
	return nil
}

func (b *synchronousBehaviour) SetVolumeLabel(ref *winfsp.FileSystemRef, _ string, info *winfsp.FSP_FSCTL_VOLUME_INFO) error {
	return b.GetVolumeInfo(ref, info)
}

func (b *synchronousBehaviour) Cleanup(ref *winfsp.FileSystemRef, file uintptr, name string, flags uint32) {
	var info winfsp.FSP_FSCTL_FILE_INFO
	if err := b.Flush(ref, file, &info); err != nil {
		b.cloud.mu.Lock()
		b.cloud.syncError = "Ошибка сохранения при закрытии файла. Данные оставлены в локальном кэше."
		b.cloud.mu.Unlock()
		return
	}
	b.bridgeBehaviour.Cleanup(ref, file, name, flags)
}

func (b *synchronousBehaviour) CanDelete(ref *winfsp.FileSystemRef, file uintptr, name string) error {
	// Fail synchronously while Windows can still report the error, before Cleanup.
	if err := b.cloud.requireCapability("delete"); err != nil {
		return err
	}
	return b.bridgeBehaviour.CanDelete(ref, file, name)
}
