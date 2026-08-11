//go:build !windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

type instanceLock struct{ path string }

func acquireSingleInstance() (*instanceLock, error) {
	directory := os.TempDir()
	path := filepath.Join(directory, "cloud-storage-core-v2.lock")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, errors.New("Cloud Storage Core is already running")
	}
	_, _ = file.WriteString(strconv.Itoa(os.Getpid()))
	_ = file.Close()
	return &instanceLock{path: path}, nil
}

func (lock *instanceLock) Close() error { return os.Remove(lock.path) }

func configureCommand(command *exec.Cmd) { command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true} }

func repairSecretPermissions(_ string) error { return nil }

func processIsCompatibilityCore(pid int, legacyPath string) bool {
	payload, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "cmdline"))
	if err != nil {
		return false
	}
	return strings.Contains(string(payload), filepath.Base(legacyPath))
}

func isWindowsService() (bool, error) { return false, nil }
func runWindowsService() error        { return errors.New("Windows services are unavailable") }
func installService() error           { return errors.New("use the systemd unit on this platform") }
func removeService() error            { return errors.New("use the systemd unit on this platform") }
func startService() error             { return errors.New("use the systemd unit on this platform") }
func stopService() error              { return errors.New("use the systemd unit on this platform") }
