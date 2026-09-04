package main

import "github.com/winfsp/go-winfsp"

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
