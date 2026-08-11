package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

const (
	defaultPublicPort   = 8765
	defaultInternalPort = 18765
)

type coreConfig struct {
	DataDirectory string
	PublicAddress string
	InternalHost  string
	InternalPort  int
	LegacyPath    string
	LogPath       string
}

type persistedConfig struct {
	Host string `json:"host"`
	Port int    `json:"port"`
}

func loadConfig() (coreConfig, error) {
	dataDirectory := strings.TrimSpace(os.Getenv("CLOUD_STORAGE_CORE_DATA_DIR"))
	if dataDirectory == "" {
		if runtime.GOOS == "windows" {
			dataDirectory = filepath.Join(os.Getenv("ProgramData"), "CloudStorage")
		} else if stateHome := strings.TrimSpace(os.Getenv("XDG_STATE_HOME")); stateHome != "" {
			dataDirectory = filepath.Join(stateHome, "cloud-storage")
		} else {
			home, err := os.UserHomeDir()
			if err != nil {
				return coreConfig{}, fmt.Errorf("resolve data directory: %w", err)
			}
			dataDirectory = filepath.Join(home, ".local", "state", "cloud-storage")
		}
	}
	if err := os.MkdirAll(dataDirectory, 0o700); err != nil {
		return coreConfig{}, fmt.Errorf("create data directory: %w", err)
	}
	if err := repairSecretPermissions(dataDirectory); err != nil {
		return coreConfig{}, err
	}

	persisted := persistedConfig{Host: "127.0.0.1", Port: defaultPublicPort}
	configPath := filepath.Join(dataDirectory, "core-config.json")
	if payload, err := os.ReadFile(configPath); err == nil {
		_ = json.Unmarshal(payload, &persisted)
	}
	if value := strings.TrimSpace(os.Getenv("CLOUD_STORAGE_CORE_HOST")); value != "" {
		persisted.Host = value
	}
	if value := strings.TrimSpace(os.Getenv("CLOUD_STORAGE_CORE_PORT")); value != "" {
		port, err := strconv.Atoi(value)
		if err != nil {
			return coreConfig{}, fmt.Errorf("invalid CLOUD_STORAGE_CORE_PORT: %w", err)
		}
		persisted.Port = port
	}
	if persisted.Host == "localhost" || persisted.Host == "::1" || persisted.Host == "" {
		persisted.Host = "127.0.0.1"
	}
	if persisted.Host != "127.0.0.1" {
		return coreConfig{}, errors.New("administrative API must use a loopback address")
	}
	if persisted.Port < 1024 || persisted.Port > 65535 {
		return coreConfig{}, errors.New("administrative API port must be between 1024 and 65535")
	}

	internalPort := defaultInternalPort
	if value := strings.TrimSpace(os.Getenv("CLOUD_STORAGE_LEGACY_PORT")); value != "" {
		port, err := strconv.Atoi(value)
		if err != nil {
			return coreConfig{}, fmt.Errorf("invalid CLOUD_STORAGE_LEGACY_PORT: %w", err)
		}
		internalPort = port
	}
	if internalPort == persisted.Port || internalPort < 1024 || internalPort > 65535 {
		internalPort = 0
	}
	if internalPort == 0 || !portAvailable("127.0.0.1", internalPort) {
		port, err := availablePort()
		if err != nil {
			return coreConfig{}, err
		}
		internalPort = port
	}

	legacyPath, err := locateLegacyCore()
	if err != nil {
		return coreConfig{}, err
	}
	return coreConfig{
		DataDirectory: dataDirectory,
		PublicAddress: net.JoinHostPort(persisted.Host, strconv.Itoa(persisted.Port)),
		InternalHost:  "127.0.0.1",
		InternalPort:  internalPort,
		LegacyPath:    legacyPath,
		LogPath:       filepath.Join(dataDirectory, "core.log"),
	}, nil
}

func locateLegacyCore() (string, error) {
	if configured := strings.TrimSpace(os.Getenv("CLOUD_STORAGE_LEGACY_CORE")); configured != "" {
		if info, err := os.Stat(configured); err == nil && !info.IsDir() {
			return configured, nil
		}
		return "", fmt.Errorf("configured compatibility core does not exist: %s", configured)
	}
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("resolve executable path: %w", err)
	}
	directory := filepath.Dir(executable)
	name := "CloudStorageLegacyCore"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	candidates := []string{
		filepath.Join(directory, name),
		filepath.Join(directory, "CloudStorageLegacyCore", name),
		filepath.Join(directory, "..", "CloudStorageLegacyCore", name),
		filepath.Join(directory, "..", "LegacyCore", name),
		filepath.Join(directory, "..", "dist", "CloudStorageLegacyCore", name),
	}
	for _, candidate := range candidates {
		absolute, _ := filepath.Abs(candidate)
		if info, statErr := os.Stat(absolute); statErr == nil && !info.IsDir() {
			return absolute, nil
		}
	}
	return "", fmt.Errorf("compatibility core %s was not found next to %s", name, executable)
}

func portAvailable(host string, port int) bool {
	listener, err := net.Listen("tcp", net.JoinHostPort(host, strconv.Itoa(port)))
	if err != nil {
		return false
	}
	_ = listener.Close()
	return true
}

func availablePort() (int, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, fmt.Errorf("select compatibility port: %w", err)
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port, nil
}
