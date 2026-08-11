//go:build windows

package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

const windowsServiceName = "CloudStorageServerCore"

type instanceLock struct{ handle windows.Handle }

func acquireSingleInstance() (*instanceLock, error) {
	name, err := windows.UTF16PtrFromString(`Global\CloudStorageCoreV2`)
	if err != nil {
		return nil, err
	}
	handle, err := windows.CreateMutex(nil, false, name)
	if err != nil {
		return nil, fmt.Errorf("create core mutex: %w", err)
	}
	if windows.GetLastError() == windows.ERROR_ALREADY_EXISTS {
		_ = windows.CloseHandle(handle)
		return nil, errors.New("Cloud Storage Core is already running")
	}
	return &instanceLock{handle: handle}, nil
}

func (lock *instanceLock) Close() error { return windows.CloseHandle(lock.handle) }

func configureCommand(command *exec.Cmd) {
	command.SysProcAttr = &windows.SysProcAttr{HideWindow: true, CreationFlags: windows.CREATE_NO_WINDOW}
}

func repairSecretPermissions(dataDirectory string) error {
	path := filepath.Join(dataDirectory, "core-secrets.json")
	if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
		return nil
	} else if err != nil {
		return fmt.Errorf("inspect Core secrets: %w", err)
	}
	command := exec.Command(
		"icacls.exe",
		path,
		"/inheritance:r",
		"/grant:r",
		"*S-1-5-18:(F)",
		"*S-1-5-32-544:(F)",
	)
	configureCommand(command)
	if output, err := command.CombinedOutput(); err != nil {
		return fmt.Errorf("repair Core secrets permissions: %w: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func processIsCompatibilityCore(pid int, legacyPath string) bool {
	process, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	handle, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		_ = process.Release()
		return false
	}
	defer windows.CloseHandle(handle)
	buffer := make([]uint16, windows.MAX_PATH*4)
	size := uint32(len(buffer))
	if err = windows.QueryFullProcessImageName(handle, 0, &buffer[0], &size); err != nil {
		return false
	}
	actual := filepath.Clean(windows.UTF16ToString(buffer[:size]))
	expected, _ := filepath.Abs(legacyPath)
	return strings.EqualFold(actual, filepath.Clean(expected))
}

type coreService struct{}

func (coreService) Execute(_ []string, requests <-chan svc.ChangeRequest, statuses chan<- svc.Status) (bool, uint32) {
	const accepts = svc.AcceptStop | svc.AcceptShutdown
	statuses <- svc.Status{State: svc.StartPending}
	config, err := loadConfig()
	if err != nil {
		return true, 2
	}
	runtime, err := newCoreRuntime(config)
	if err != nil {
		return true, 2
	}
	defer runtime.close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- runtime.run(ctx) }()
	statuses <- svc.Status{State: svc.Running, Accepts: accepts}
	for {
		select {
		case request := <-requests:
			switch request.Cmd {
			case svc.Interrogate:
				statuses <- svc.Status{State: svc.Running, Accepts: accepts}
			case svc.Stop, svc.Shutdown:
				statuses <- svc.Status{State: svc.StopPending}
				runtime.requestStop()
				cancel()
				select {
				case <-done:
				case <-time.After(15 * time.Second):
				}
				statuses <- svc.Status{State: svc.Stopped}
				return false, 0
			}
		case runErr := <-done:
			statuses <- svc.Status{State: svc.Stopped}
			if runErr != nil {
				return true, 2
			}
			return false, 0
		}
	}
}

func isWindowsService() (bool, error) { return svc.IsWindowsService() }

func runWindowsService() error { return svc.Run(windowsServiceName, coreService{}) }

func installService() error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	connection, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to Service Control Manager: %w", err)
	}
	defer connection.Disconnect()
	if existing, openErr := connection.OpenService(windowsServiceName); openErr == nil {
		defer existing.Close()
		return existing.UpdateConfig(mgr.Config{
			DisplayName:    "Cloud Storage Server Core",
			Description:    "Native Go supervisor for the Cloud Storage home server.",
			StartType:      mgr.StartAutomatic,
			BinaryPathName: fmt.Sprintf("\"%s\" service", executable),
		})
	}
	service, err := connection.CreateService(windowsServiceName, executable, mgr.Config{
		DisplayName: "Cloud Storage Server Core",
		Description: "Native Go supervisor for the Cloud Storage home server.",
		StartType:   mgr.StartAutomatic,
	}, "service")
	if err != nil {
		return fmt.Errorf("install Windows service: %w", err)
	}
	defer service.Close()
	return nil
}

func removeService() error {
	connection, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer connection.Disconnect()
	service, err := connection.OpenService(windowsServiceName)
	if err != nil {
		return nil
	}
	defer service.Close()
	return service.Delete()
}

func startService() error {
	connection, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer connection.Disconnect()
	service, err := connection.OpenService(windowsServiceName)
	if err != nil {
		return err
	}
	defer service.Close()
	return service.Start()
}

func stopService() error {
	connection, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer connection.Disconnect()
	service, err := connection.OpenService(windowsServiceName)
	if err != nil {
		return nil
	}
	defer service.Close()
	_, err = service.Control(svc.Stop)
	return err
}
