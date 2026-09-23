package main

import (
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func TestSingleInstanceKeyIsStablePerDataDirectory(t *testing.T) {
	first := singleInstanceKey(filepath.Join("data", "server"))
	if first != singleInstanceKey(filepath.Join("data", ".", "server")) {
		t.Fatal("equivalent data directories produced different Core lock keys")
	}
	if first == singleInstanceKey(filepath.Join("data", "other")) {
		t.Fatal("different data directories must not share a Core lock")
	}
}

func TestShutdownRequiresSuccessfulCoreAuthorization(t *testing.T) {
	for _, status := range []int{http.StatusUnauthorized, http.StatusForbidden, http.StatusServiceUnavailable, http.StatusOK} {
		t.Run(itoa(status), func(t *testing.T) {
			backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
			}))
			defer backend.Close()
			address := backend.Listener.Addr().(*net.TCPAddr)
			runtime := &coreRuntime{config: coreConfig{InternalHost: "127.0.0.1", InternalPort: address.Port}}
			var cancelled atomic.Bool
			proxy := runtime.proxyServer(func() { cancelled.Store(true) })
			response := httptest.NewRecorder()
			proxy.Handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/v1/admin/shutdown", nil))
			if response.Code != status {
				t.Fatalf("status %d, want %d", response.Code, status)
			}
			if runtime.stop.Load() != (status == http.StatusOK) {
				t.Errorf("shutdown scheduled=%v after backend status %d", runtime.stop.Load(), status)
			}
			time.Sleep(900 * time.Millisecond)
			if cancelled.Load() != (status == http.StatusOK) {
				t.Errorf("supervisor cancelled=%v after backend status %d", cancelled.Load(), status)
			}
		})
	}
}

func TestFilteredEnvironmentReplacesCoreValues(t *testing.T) {
	values := filteredEnvironment([]string{"PATH=x", "CLOUD_STORAGE_CORE_PORT=8765", "cloud_storage_core_host=localhost"}, "CLOUD_STORAGE_CORE_PORT", "CLOUD_STORAGE_CORE_HOST")
	if len(values) != 1 || values[0] != "PATH=x" {
		t.Fatalf("unexpected filtered environment: %#v", values)
	}
}

func TestAvailablePortCanBeBound(t *testing.T) {
	port, err := availablePort()
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", itoa(port)))
	if err != nil {
		t.Fatalf("selected port cannot be rebound: %v", err)
	}
	_ = listener.Close()
}

func TestLocateLegacyCoreFromEnvironment(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "legacy-core")
	if err := os.WriteFile(path, []byte("test"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CLOUD_STORAGE_LEGACY_CORE", path)
	actual, err := locateLegacyCore()
	if err != nil {
		t.Fatal(err)
	}
	if actual != path {
		t.Fatalf("got %q, want %q", actual, path)
	}
}

func TestStalePIDBelongingToAnotherProcessIsRemoved(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "core.pid")
	if err := os.WriteFile(path, []byte(itoa(os.Getpid())), 0o600); err != nil {
		t.Fatal(err)
	}
	runtime := &coreRuntime{
		config: coreConfig{DataDirectory: directory, LegacyPath: filepath.Join(directory, "CloudStorageLegacyCore.exe")},
		logger: log.New(io.Discard, "", 0),
	}
	if err := runtime.removeStalePID(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("stale PID was not removed: %v", err)
	}
}

func itoa(value int) string {
	result := ""
	for value > 0 {
		result = string(rune('0'+value%10)) + result
		value /= 10
	}
	return result
}
