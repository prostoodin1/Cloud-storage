package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const coreVersion = "0.9.22"

type coreRuntime struct {
	config coreConfig
	logger *log.Logger
	logOut io.Closer
	cancel context.CancelFunc
	stop   atomic.Bool
	once   sync.Once
}

func newCoreRuntime(config coreConfig) (*coreRuntime, error) {
	logFile, err := os.OpenFile(config.LogPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, fmt.Errorf("open core log: %w", err)
	}
	return &coreRuntime{
		config: config,
		logger: log.New(logFile, "GO-CORE ", log.Ldate|log.Ltime|log.LUTC),
		logOut: logFile,
	}, nil
}

func (runtime *coreRuntime) close() {
	if runtime.logOut != nil {
		_ = runtime.logOut.Close()
	}
}

func (runtime *coreRuntime) run(parent context.Context) error {
	instance, err := acquireSingleInstance()
	if err != nil {
		return err
	}
	defer instance.Close()
	ctx, cancel := context.WithCancel(parent)
	runtime.cancel = cancel
	defer cancel()

	if healthyURL("http://"+runtime.config.PublicAddress+"/v1/health", 300*time.Millisecond) {
		return errors.New("another Cloud Storage Core API is already listening")
	}
	if err := runtime.removeStalePID(); err != nil {
		return err
	}
	runtime.logger.Printf("native supervisor %s starting; public=%s compatibility=%s:%d", coreVersion, runtime.config.PublicAddress, runtime.config.InternalHost, runtime.config.InternalPort)

	childErrors := make(chan error, 1)
	go func() { childErrors <- runtime.superviseCompatibilityCore(ctx) }()

	serverErrors := make(chan error, 1)
	server := runtime.proxyServer(cancel)
	go func() {
		err := server.ListenAndServe()
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		serverErrors <- err
	}()

	var result error
	select {
	case <-ctx.Done():
	case err = <-childErrors:
		if err != nil && !runtime.stop.Load() {
			result = err
		}
		cancel()
	case err = <-serverErrors:
		if err != nil {
			result = fmt.Errorf("administrative proxy failed: %w", err)
		}
		cancel()
	}

	shutdownContext, shutdownCancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer shutdownCancel()
	_ = server.Shutdown(shutdownContext)
	runtime.logger.Printf("native supervisor stopped")
	return result
}

func (runtime *coreRuntime) proxyServer(cancel context.CancelFunc) *http.Server {
	target, _ := url.Parse(fmt.Sprintf("http://%s:%d", runtime.config.InternalHost, runtime.config.InternalPort))
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.ModifyResponse = func(response *http.Response) error {
		if response.Request.URL.Path != "/v1/health" || response.StatusCode != http.StatusOK {
			return nil
		}
		payload, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		_ = response.Body.Close()
		if err != nil {
			return err
		}
		body := map[string]any{}
		if err = json.Unmarshal(payload, &body); err != nil {
			return err
		}
		body["runtime"] = "go"
		body["supervisor_version"] = coreVersion
		payload, err = json.Marshal(body)
		if err != nil {
			return err
		}
		response.Body = io.NopCloser(bytes.NewReader(payload))
		response.ContentLength = int64(len(payload))
		response.Header.Set("Content-Length", strconv.Itoa(len(payload)))
		return nil
	}
	proxy.ErrorHandler = func(writer http.ResponseWriter, request *http.Request, err error) {
		writer.Header().Set("Content-Type", "application/json; charset=utf-8")
		writer.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(writer, `{"detail":"Cloud Storage compatibility engine is starting","runtime":"go","version":"`+coreVersion+`"}`)
	}
	handler := http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("X-Cloud-Storage-Runtime", "go/"+coreVersion)
		proxy.ServeHTTP(writer, request)
		if request.Method == http.MethodPost && request.URL.Path == "/v1/admin/shutdown" {
			// The compatibility endpoint schedules its own graceful Uvicorn
			// shutdown after returning. Give it a short window to flush SQLite
			// and logs before the supervisor context terminates the process.
			runtime.stop.Store(true)
			go func() {
				time.Sleep(750 * time.Millisecond)
				runtime.requestStop()
				cancel()
			}()
		}
	})
	return &http.Server{
		Addr:              runtime.config.PublicAddress,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}
}

func (runtime *coreRuntime) superviseCompatibilityCore(ctx context.Context) error {
	backoff := time.Second
	for {
		if ctx.Err() != nil || runtime.stop.Load() {
			return nil
		}
		exitCode, err := runtime.runCompatibilityCore(ctx)
		if ctx.Err() != nil || runtime.stop.Load() {
			return nil
		}
		if err != nil {
			runtime.logger.Printf("compatibility engine stopped unexpectedly: %v (exit=%d); restart in %s", err, exitCode, backoff)
		} else {
			runtime.logger.Printf("compatibility engine exited unexpectedly with code %d; restart in %s", exitCode, backoff)
		}
		timer := time.NewTimer(backoff)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil
		case <-timer.C:
		}
		if backoff < 15*time.Second {
			backoff *= 2
		}
	}
}

func (runtime *coreRuntime) runCompatibilityCore(ctx context.Context) (int, error) {
	if err := runtime.removeStalePID(); err != nil {
		return -1, err
	}
	command := exec.CommandContext(ctx, runtime.config.LegacyPath)
	configureCommand(command)
	command.Dir = runtime.config.DataDirectory
	command.Env = append(filteredEnvironment(os.Environ(), "CLOUD_STORAGE_CORE_HOST", "CLOUD_STORAGE_CORE_PORT", "CLOUD_STORAGE_LEGACY_PORT"),
		"CLOUD_STORAGE_CORE_DATA_DIR="+runtime.config.DataDirectory,
		"CLOUD_STORAGE_CORE_HOST="+runtime.config.InternalHost,
		"CLOUD_STORAGE_CORE_PORT="+strconv.Itoa(runtime.config.InternalPort),
		"CLOUD_STORAGE_RUNTIME_OWNER=go",
	)
	logFile, err := os.OpenFile(runtime.config.LogPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return -1, fmt.Errorf("open compatibility log: %w", err)
	}
	defer logFile.Close()
	command.Stdout = logFile
	command.Stderr = logFile
	command.Stdin = nil
	if err = command.Start(); err != nil {
		return -1, fmt.Errorf("start compatibility engine: %w", err)
	}
	runtime.logger.Printf("compatibility engine started pid=%d port=%d", command.Process.Pid, runtime.config.InternalPort)
	waitError := command.Wait()
	_ = runtime.removePIDForProcess(command.Process.Pid)
	if waitError == nil {
		return 0, nil
	}
	var exitError *exec.ExitError
	if errors.As(waitError, &exitError) {
		return exitError.ExitCode(), waitError
	}
	return -1, waitError
}

func (runtime *coreRuntime) requestStop() {
	runtime.once.Do(func() {
		runtime.stop.Store(true)
		if runtime.cancel != nil {
			runtime.cancel()
		}
	})
}

func (runtime *coreRuntime) removeStalePID() error {
	path := filepath.Join(runtime.config.DataDirectory, "core.pid")
	payload, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read compatibility PID: %w", err)
	}
	pid, parseErr := strconv.Atoi(strings.TrimSpace(string(payload)))
	if parseErr != nil || pid <= 0 {
		return os.Remove(path)
	}
	if processIsCompatibilityCore(pid, runtime.config.LegacyPath) {
		return fmt.Errorf("compatibility core is already running with PID %d", pid)
	}
	if err = os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove stale compatibility PID: %w", err)
	}
	runtime.logger.Printf("removed stale compatibility PID %d", pid)
	return nil
}

func (runtime *coreRuntime) removePIDForProcess(pid int) error {
	path := filepath.Join(runtime.config.DataDirectory, "core.pid")
	payload, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	stored, _ := strconv.Atoi(strings.TrimSpace(string(payload)))
	if stored == pid {
		return os.Remove(path)
	}
	return nil
}

func healthyURL(address string, timeout time.Duration) bool {
	client := &http.Client{Timeout: timeout}
	response, err := client.Get(address)
	if err != nil {
		return false
	}
	defer response.Body.Close()
	return response.StatusCode >= 200 && response.StatusCode < 300
}

func filteredEnvironment(values []string, names ...string) []string {
	blocked := make(map[string]struct{}, len(names))
	for _, name := range names {
		blocked[strings.ToUpper(name)] = struct{}{}
	}
	result := make([]string, 0, len(values))
	for _, value := range values {
		name, _, _ := strings.Cut(value, "=")
		if _, found := blocked[strings.ToUpper(name)]; !found {
			result = append(result, value)
		}
	}
	return result
}
