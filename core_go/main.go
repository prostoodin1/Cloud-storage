package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
)

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(arguments []string) int {
	command := lastCommand(arguments)
	switch command {
	case "--smoke-test":
		return 0
	case "--version", "version":
		fmt.Println(coreVersion)
		return 0
	case "install":
		if err := installService(); err != nil {
			return fail(err)
		}
		return 0
	case "remove", "uninstall":
		if err := removeService(); err != nil {
			return fail(err)
		}
		return 0
	case "start":
		if err := startService(); err != nil {
			return fail(err)
		}
		return 0
	case "stop":
		if err := stopService(); err != nil {
			return fail(err)
		}
		return 0
	}

	config, err := loadConfig()
	if err != nil {
		return fail(err)
	}
	if command == "--initialize-only" {
		process := exec.Command(config.LegacyPath, "--initialize-only")
		configureCommand(process)
		process.Env = append(filteredEnvironment(os.Environ(), "CLOUD_STORAGE_CORE_DATA_DIR"), "CLOUD_STORAGE_CORE_DATA_DIR="+config.DataDirectory)
		if output, runErr := process.CombinedOutput(); runErr != nil {
			return fail(fmt.Errorf("initialize compatibility data: %w: %s", runErr, strings.TrimSpace(string(output))))
		}
		return 0
	}

	service, err := isWindowsService()
	if err != nil {
		return fail(err)
	}
	if service {
		if err = runWindowsService(); err != nil {
			return fail(err)
		}
		return 0
	}

	runtime, err := newCoreRuntime(config)
	if err != nil {
		return fail(err)
	}
	defer runtime.close()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err = runtime.run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		return fail(err)
	}
	return 0
}

func lastCommand(arguments []string) string {
	known := map[string]bool{
		"--smoke-test": true, "--initialize-only": true, "--version": true,
		"version": true, "install": true, "remove": true, "uninstall": true,
		"start": true, "stop": true,
	}
	for index := len(arguments) - 1; index >= 0; index-- {
		if known[strings.ToLower(arguments[index])] {
			return strings.ToLower(arguments[index])
		}
	}
	return ""
}

func fail(err error) int {
	_, _ = fmt.Fprintln(os.Stderr, "Cloud Storage Go Core:", err)
	return 2
}
