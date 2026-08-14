//go:build windows

package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/winfsp/go-winfsp"
	"github.com/winfsp/go-winfsp/gofs"
	"golang.org/x/sys/windows"
)

const (
	maxResponseBytes = 16 * 1024 * 1024
	stillActive      = 259
	driveMutexPrefix = `Local\CloudStorageDrive-`
)

type settingsDocument struct {
	ActiveProfileID string    `json:"active_profile_id"`
	Profiles        []profile `json:"profiles"`
}

type profile struct {
	ProfileID              string `json:"profile_id"`
	ServerURL              string `json:"server_url"`
	ServerName             string `json:"server_name"`
	CertificateFingerprint string `json:"certificate_fingerprint"`
	DeviceStatus           string `json:"device_status"`
	LastSpaceID            string `json:"last_space_id"`
	DriveLetter            string `json:"drive_letter"`
}

type space struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

type entry struct {
	Name       string `json:"name"`
	Type       string `json:"type"`
	SizeBytes  int64  `json:"size_bytes"`
	SHA256     string `json:"sha256"`
	ModifiedAt string `json:"modified_at"`
}

type statusDocument struct {
	SchemaVersion int    `json:"schema_version"`
	ProfileID     string `json:"profile_id"`
	SpaceID       string `json:"space_id"`
	DriveLetter   string `json:"drive_letter"`
	State         string `json:"state"`
	Detail        string `json:"detail"`
	PID           int    `json:"pid"`
	UpdatedAt     string `json:"updated_at"`
}

type apiClient struct {
	baseURL       *url.URL
	token         string
	remoteSession string
	http          *http.Client
}

func newAPIClient(p profile, token, remoteSession string) (*apiClient, error) {
	base, err := url.Parse(strings.TrimRight(p.ServerURL, "/"))
	if err != nil || base.Hostname() == "" || (base.Scheme != "http" && base.Scheme != "https") {
		return nil, errors.New("invalid server URL")
	}
	if base.Scheme == "http" && !isLoopback(base.Hostname()) {
		return nil, errors.New("remote server requires HTTPS")
	}
	transport := &http.Transport{Proxy: http.ProxyFromEnvironment}
	fingerprint := strings.ToLower(strings.ReplaceAll(p.CertificateFingerprint, ":", ""))
	if base.Scheme == "https" && fingerprint != "" {
		if len(fingerprint) != 64 {
			return nil, errors.New("invalid certificate fingerprint")
		}
		transport.TLSClientConfig = &tls.Config{
			MinVersion:         tls.VersionTLS12,
			InsecureSkipVerify: true, // Exact SHA-256 pin is checked below.
			VerifyConnection: func(state tls.ConnectionState) error {
				if len(state.PeerCertificates) == 0 {
					return errors.New("server did not provide a certificate")
				}
				actual := sha256.Sum256(state.PeerCertificates[0].Raw)
				if hex.EncodeToString(actual[:]) != fingerprint {
					return errors.New("server certificate fingerprint mismatch")
				}
				return nil
			},
		}
	} else if base.Scheme == "https" {
		transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: systemRoots()}
	}
	return &apiClient{
		baseURL:       base,
		token:         token,
		remoteSession: remoteSession,
		http: &http.Client{
			Transport: transport,
			Timeout:   90 * time.Second,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}, nil
}

func systemRoots() *x509.CertPool {
	pool, _ := x509.SystemCertPool()
	return pool
}

func (a *apiClient) request(method, route string, body io.Reader, contentType string) ([]byte, error) {
	target, err := a.routeURL(route)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest(method, target, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Authorization", "Bearer "+a.token)
	if a.remoteSession != "" {
		req.Header.Set("X-Cloud-Remote-Session", a.remoteSession)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	response, err := a.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return nil, err
	}
	if len(payload) > maxResponseBytes {
		return nil, errors.New("server response too large")
	}
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return nil, apiStatusError(response.StatusCode, payload)
	}
	return payload, nil
}

func (a *apiClient) routeURL(route string) (string, error) {
	relative, err := url.ParseRequestURI(route)
	if err != nil || relative.IsAbs() || relative.Host != "" || !strings.HasPrefix(relative.Path, "/") {
		return "", errors.New("invalid API route")
	}
	target := *a.baseURL
	target.Path = strings.TrimRight(a.baseURL.Path, "/") + relative.Path
	target.RawPath = strings.TrimRight(a.baseURL.EscapedPath(), "/") + relative.EscapedPath()
	target.RawQuery = relative.RawQuery
	return target.String(), nil
}

func apiStatusError(code int, payload []byte) error {
	var value struct {
		Detail string `json:"detail"`
	}
	_ = json.Unmarshal(payload, &value)
	switch code {
	case http.StatusNotFound:
		return fmt.Errorf("%w: %s", os.ErrNotExist, value.Detail)
	case http.StatusConflict:
		return fmt.Errorf("%w: %s", os.ErrExist, value.Detail)
	case http.StatusUnauthorized, http.StatusForbidden:
		return fmt.Errorf("%w: %s", os.ErrPermission, value.Detail)
	default:
		return fmt.Errorf("server returned HTTP %d: %s", code, value.Detail)
	}
}

func (a *apiClient) listSpaces() ([]space, error) {
	payload, err := a.request(http.MethodGet, "/v1/spaces", nil, "")
	if err != nil {
		return nil, err
	}
	var result []space
	err = json.Unmarshal(payload, &result)
	return result, err
}

func (a *apiClient) listEntries(spaceID, directory string) ([]entry, error) {
	route := "/v1/spaces/" + url.PathEscape(spaceID) + "/entries?directory=" + url.QueryEscape(directory)
	payload, err := a.request(http.MethodGet, route, nil, "")
	if err != nil {
		return nil, err
	}
	var result []entry
	err = json.Unmarshal(payload, &result)
	return result, err
}

func (a *apiClient) download(spaceID, logicalPath, expectedHash string, destination string) error {
	target, err := a.routeURL(fileRoute(spaceID, logicalPath))
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodGet, target, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+a.token)
	if a.remoteSession != "" {
		req.Header.Set("X-Cloud-Remote-Session", a.remoteSession)
	}
	response, err := a.http.Do(req)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		payload, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
		return apiStatusError(response.StatusCode, payload)
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o700); err != nil {
		return err
	}
	temporary := destination + ".download"
	handle, err := os.OpenFile(temporary, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	digest := sha256.New()
	_, copyErr := io.Copy(io.MultiWriter(handle, digest), response.Body)
	syncErr := handle.Sync()
	closeErr := handle.Close()
	if copyErr != nil || syncErr != nil || closeErr != nil {
		_ = os.Remove(temporary)
		return errors.Join(copyErr, syncErr, closeErr)
	}
	if expectedHash != "" && hex.EncodeToString(digest.Sum(nil)) != strings.ToLower(expectedHash) {
		_ = os.Remove(temporary)
		return errors.New("download SHA-256 mismatch")
	}
	return os.Rename(temporary, destination)
}

func (a *apiClient) upload(spaceID, logicalPath, source string) error {
	handle, err := os.Open(source)
	if err != nil {
		return err
	}
	defer handle.Close()
	stat, err := handle.Stat()
	if err != nil {
		return err
	}
	target, err := a.routeURL(fileRoute(spaceID, logicalPath))
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodPut, target, handle)
	if err != nil {
		return err
	}
	req.ContentLength = stat.Size()
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("Authorization", "Bearer "+a.token)
	if a.remoteSession != "" {
		req.Header.Set("X-Cloud-Remote-Session", a.remoteSession)
	}
	response, err := a.http.Do(req)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	payload, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return apiStatusError(response.StatusCode, payload)
	}
	return nil
}

func (a *apiClient) createDirectory(spaceID, logicalPath string) error {
	body, _ := json.Marshal(map[string]string{"logical_path": logicalPath})
	_, err := a.request(http.MethodPost, "/v1/spaces/"+url.PathEscape(spaceID)+"/directories", bytes.NewReader(body), "application/json")
	return err
}

func (a *apiClient) deleteEntry(spaceID, logicalPath, kind string) error {
	var route string
	if kind == "directory" {
		route = "/v1/spaces/" + url.PathEscape(spaceID) + "/directories/" + escapeLogicalPath(logicalPath)
	} else {
		route = fileRoute(spaceID, logicalPath)
	}
	_, err := a.request(http.MethodDelete, route, nil, "")
	return err
}

func (a *apiClient) move(spaceID, source, destination, kind string) error {
	body, _ := json.Marshal(map[string]string{
		"source_path": source, "destination_path": destination, "kind": kind,
	})
	_, err := a.request(http.MethodPost, "/v1/spaces/"+url.PathEscape(spaceID)+"/moves", bytes.NewReader(body), "application/json")
	return err
}

func fileRoute(spaceID, logicalPath string) string {
	return "/v1/spaces/" + url.PathEscape(spaceID) + "/files/" + escapeLogicalPath(logicalPath)
}

func escapeLogicalPath(value string) string {
	parts := strings.Split(value, "/")
	for index := range parts {
		parts[index] = url.PathEscape(parts[index])
	}
	return strings.Join(parts, "/")
}

type cloudFileSystem struct {
	api         *apiClient
	spaceID     string
	cacheRoot   string
	cacheHashes map[string]string
	mu          sync.Mutex
}

func newCloudFileSystem(api *apiClient, spaceID, cacheRoot string) *cloudFileSystem {
	return &cloudFileSystem{api: api, spaceID: spaceID, cacheRoot: cacheRoot, cacheHashes: map[string]string{}}
}

func (c *cloudFileSystem) OpenFile(name string, flags int, perm os.FileMode) (gofs.File, error) {
	logical, err := normalizeName(name)
	if err != nil {
		return nil, err
	}
	info, statErr := c.Stat(logical)
	if statErr == nil && info.IsDir() {
		entries, err := c.api.listEntries(c.spaceID, logical)
		if err != nil {
			return nil, err
		}
		return newDirectoryHandle(info, entries), nil
	}
	if statErr != nil && !errors.Is(statErr, os.ErrNotExist) {
		return nil, statErr
	}
	writable := flags&(os.O_WRONLY|os.O_RDWR|os.O_CREATE|os.O_TRUNC|os.O_APPEND) != 0
	if statErr != nil && flags&os.O_CREATE == 0 {
		return nil, statErr
	}
	local, err := c.cachePath(logical)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(local), 0o700); err != nil {
		return nil, err
	}
	if statErr == nil && info.Mode().IsRegular() && flags&os.O_TRUNC == 0 {
		remote := info.(*remoteInfo)
		if err := c.ensureCached(logical, remote, local); err != nil {
			return nil, err
		}
	}
	handle, err := os.OpenFile(local, flags, perm)
	if err != nil {
		return nil, err
	}
	return &trackedFile{File: handle, owner: c, logicalPath: logical, dirty: writable}, nil
}

func (c *cloudFileSystem) Mkdir(name string, _ os.FileMode) error {
	logical, err := normalizeName(name)
	if err != nil || logical == "" {
		return syscall.EINVAL
	}
	return c.api.createDirectory(c.spaceID, logical)
}

func (c *cloudFileSystem) Stat(name string) (os.FileInfo, error) {
	logical, err := normalizeName(name)
	if err != nil {
		return nil, err
	}
	if logical == "" {
		return &remoteInfo{name: "", directory: true, modified: time.Now()}, nil
	}
	parent, base := path.Split(logical)
	entries, err := c.api.listEntries(c.spaceID, strings.TrimSuffix(parent, "/"))
	if err != nil {
		return nil, err
	}
	for _, candidate := range entries {
		if strings.EqualFold(candidate.Name, base) {
			return infoFromEntry(candidate), nil
		}
	}
	return nil, os.ErrNotExist
}

func (c *cloudFileSystem) Rename(source, target string) error {
	sourcePath, err := normalizeName(source)
	if err != nil {
		return err
	}
	targetPath, err := normalizeName(target)
	if err != nil {
		return err
	}
	info, err := c.Stat(sourcePath)
	if err != nil {
		return err
	}
	kind := "file"
	if info.IsDir() {
		kind = "directory"
	}
	if err := c.api.move(c.spaceID, sourcePath, targetPath, kind); err != nil {
		return err
	}
	sourceLocal, _ := c.cachePath(sourcePath)
	targetLocal, _ := c.cachePath(targetPath)
	_ = os.MkdirAll(filepath.Dir(targetLocal), 0o700)
	_ = os.Rename(sourceLocal, targetLocal)
	c.mu.Lock()
	delete(c.cacheHashes, strings.ToLower(sourcePath))
	c.mu.Unlock()
	return nil
}

func (c *cloudFileSystem) Remove(name string) error {
	logical, err := normalizeName(name)
	if err != nil {
		return err
	}
	info, err := c.Stat(logical)
	if err != nil {
		return err
	}
	kind := "file"
	if info.IsDir() {
		kind = "directory"
	}
	if err := c.api.deleteEntry(c.spaceID, logical, kind); err != nil {
		return err
	}
	local, _ := c.cachePath(logical)
	_ = os.Remove(local)
	c.mu.Lock()
	delete(c.cacheHashes, strings.ToLower(logical))
	c.mu.Unlock()
	return nil
}

func (c *cloudFileSystem) cachePath(logical string) (string, error) {
	clean := filepath.FromSlash(logical)
	target := filepath.Join(c.cacheRoot, clean)
	relative, err := filepath.Rel(c.cacheRoot, target)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return "", os.ErrPermission
	}
	return target, nil
}

func (c *cloudFileSystem) ensureCached(logical string, info *remoteInfo, local string) error {
	c.mu.Lock()
	hash := c.cacheHashes[strings.ToLower(logical)]
	c.mu.Unlock()
	if stat, err := os.Stat(local); err == nil && stat.Size() == info.size && hash == info.sha256 {
		return nil
	}
	if err := c.api.download(c.spaceID, logical, info.sha256, local); err != nil {
		return err
	}
	c.mu.Lock()
	c.cacheHashes[strings.ToLower(logical)] = info.sha256
	c.mu.Unlock()
	return nil
}

func (c *cloudFileSystem) upload(logical, local string) error {
	if err := c.api.upload(c.spaceID, logical, local); err != nil {
		return err
	}
	c.mu.Lock()
	delete(c.cacheHashes, strings.ToLower(logical))
	c.mu.Unlock()
	return nil
}

type trackedFile struct {
	*os.File
	owner       *cloudFileSystem
	logicalPath string
	dirty       bool
	closed      bool
	mu          sync.Mutex
}

func (t *trackedFile) Write(payload []byte) (int, error) {
	t.dirty = true
	return t.File.Write(payload)
}

func (t *trackedFile) WriteAt(payload []byte, offset int64) (int, error) {
	t.dirty = true
	return t.File.WriteAt(payload, offset)
}

func (t *trackedFile) Truncate(size int64) error {
	t.dirty = true
	return t.File.Truncate(size)
}

func (t *trackedFile) Sync() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if err := t.File.Sync(); err != nil {
		return err
	}
	if !t.dirty {
		return nil
	}
	if err := t.owner.upload(t.logicalPath, t.File.Name()); err != nil {
		return err
	}
	t.dirty = false
	return nil
}

func (t *trackedFile) Close() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return nil
	}
	if err := t.File.Sync(); err != nil {
		_ = t.File.Close()
		t.closed = true
		return err
	}
	if t.dirty {
		if err := t.owner.upload(t.logicalPath, t.File.Name()); err != nil {
			_ = t.File.Close()
			t.closed = true
			return err
		}
	}
	t.closed = true
	return t.File.Close()
}

type remoteInfo struct {
	name      string
	size      int64
	directory bool
	modified  time.Time
	sha256    string
}

func (r *remoteInfo) Name() string       { return r.name }
func (r *remoteInfo) Size() int64        { return r.size }
func (r *remoteInfo) ModTime() time.Time { return r.modified }
func (r *remoteInfo) IsDir() bool        { return r.directory }
func (r *remoteInfo) Sys() any           { return nil }
func (r *remoteInfo) Mode() fs.FileMode {
	if r.directory {
		return fs.ModeDir | 0o700
	}
	return 0o600
}

func infoFromEntry(value entry) *remoteInfo {
	modified, err := time.Parse(time.RFC3339Nano, value.ModifiedAt)
	if err != nil {
		modified = time.Now()
	}
	return &remoteInfo{
		name: value.Name, size: value.SizeBytes, directory: value.Type == "directory",
		modified: modified, sha256: strings.ToLower(value.SHA256),
	}
}

type directoryHandle struct {
	info    os.FileInfo
	entries []os.FileInfo
	index   int
}

func newDirectoryHandle(info os.FileInfo, entries []entry) *directoryHandle {
	converted := make([]os.FileInfo, 0, len(entries))
	for _, item := range entries {
		converted = append(converted, infoFromEntry(item))
	}
	return &directoryHandle{info: info, entries: converted}
}

func (d *directoryHandle) Read([]byte) (int, error)           { return 0, syscall.EISDIR }
func (d *directoryHandle) Write([]byte) (int, error)          { return 0, syscall.EISDIR }
func (d *directoryHandle) ReadAt([]byte, int64) (int, error)  { return 0, syscall.EISDIR }
func (d *directoryHandle) WriteAt([]byte, int64) (int, error) { return 0, syscall.EISDIR }
func (d *directoryHandle) Seek(int64, int) (int64, error)     { return 0, syscall.EISDIR }
func (d *directoryHandle) Sync() error                        { return nil }
func (d *directoryHandle) Truncate(int64) error               { return syscall.EISDIR }
func (d *directoryHandle) Close() error                       { return nil }
func (d *directoryHandle) Stat() (os.FileInfo, error)         { return d.info, nil }
func (d *directoryHandle) Readdir(count int) ([]os.FileInfo, error) {
	if d.index >= len(d.entries) {
		return nil, io.EOF
	}
	end := len(d.entries)
	if count > 0 && d.index+count < end {
		end = d.index + count
	}
	result := d.entries[d.index:end]
	d.index = end
	return result, nil
}

func normalizeName(name string) (string, error) {
	value := strings.ReplaceAll(name, "\\", "/")
	value = strings.TrimLeft(value, "/")
	if value == "" || value == "." {
		return "", nil
	}
	clean := path.Clean(value)
	if clean == ".." || strings.HasPrefix(clean, "../") || strings.ContainsRune(clean, '\x00') {
		return "", os.ErrPermission
	}
	for _, segment := range strings.Split(clean, "/") {
		if segment == "" || segment == "." || segment == ".." || strings.HasSuffix(segment, " ") || strings.HasSuffix(segment, ".") {
			return "", syscall.EINVAL
		}
	}
	return clean, nil
}

func loadProfile(dataDir, profileID string) (profile, string, string, error) {
	payload, err := os.ReadFile(filepath.Join(dataDir, "client-settings.json"))
	if err != nil {
		return profile{}, "", "", err
	}
	var document settingsDocument
	if err := json.Unmarshal(payload, &document); err != nil {
		return profile{}, "", "", err
	}
	for _, candidate := range document.Profiles {
		if candidate.ProfileID != profileID {
			continue
		}
		token, err := readProtectedToken(filepath.Join(dataDir, "tokens", profileID+".bin"), "csd_")
		if err != nil {
			return profile{}, "", "", err
		}
		remote, _ := readProtectedToken(filepath.Join(dataDir, "sessions", profileID+".bin"), "css_")
		return candidate, token, remote, nil
	}
	return profile{}, "", "", os.ErrNotExist
}

type dataBlob struct {
	length uint32
	data   *byte
}

var (
	crypt32            = windows.NewLazySystemDLL("crypt32.dll")
	cryptUnprotectData = crypt32.NewProc("CryptUnprotectData")
	kernel32           = windows.NewLazySystemDLL("kernel32.dll")
	localFree          = kernel32.NewProc("LocalFree")
)

func readProtectedToken(filename, prefix string) (string, error) {
	payload, err := os.ReadFile(filename)
	if err != nil {
		return "", err
	}
	if len(payload) == 0 {
		return "", errors.New("empty protected token")
	}
	input := dataBlob{length: uint32(len(payload)), data: &payload[0]}
	var output dataBlob
	result, _, callErr := cryptUnprotectData.Call(
		uintptr(unsafe.Pointer(&input)), 0, 0, 0, 0, 0, uintptr(unsafe.Pointer(&output)),
	)
	if result == 0 {
		return "", callErr
	}
	defer localFree.Call(uintptr(unsafe.Pointer(output.data)))
	plain := unsafe.Slice(output.data, output.length)
	token := string(plain)
	if !strings.HasPrefix(token, prefix) {
		return "", errors.New("protected token has an invalid prefix")
	}
	return token, nil
}

func writeStatus(path string, value statusDocument) {
	value.SchemaVersion = 1
	value.PID = os.Getpid()
	value.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	_ = os.MkdirAll(filepath.Dir(path), 0o700)
	payload, _ := json.MarshalIndent(value, "", "  ")
	temporary := path + ".tmp"
	if os.WriteFile(temporary, payload, 0o600) == nil {
		_ = os.Rename(temporary, path)
	}
}

func parentAlive(pid int) bool {
	if pid <= 0 {
		return true
	}
	handle, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(handle)
	var code uint32
	return windows.GetExitCodeProcess(handle, &code) == nil && code == stillActive
}

func isLoopback(host string) bool {
	host = strings.ToLower(host)
	return host == "localhost" || host == "127.0.0.1" || host == "::1"
}

func run() error {
	profileID := flag.String("profile-id", "", "client profile identifier")
	spaceFlag := flag.String("space-id", "", "logical space identifier")
	mountpoint := flag.String("mount", "S:", "drive letter mount point")
	dataDir := flag.String("data-dir", "", "Cloud Storage Client data directory")
	parentPID := flag.Int("parent-pid", 0, "parent client process identifier")
	smokeTest := flag.Bool("smoke-test", false, "validate the executable and exit")
	flag.Parse()
	if *smokeTest {
		return nil
	}
	if !regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`).MatchString(*profileID) {
		return errors.New("invalid profile identifier")
	}
	if !regexp.MustCompile(`^[A-Za-z0-9_-]{1,100}$`).MatchString(*spaceFlag) {
		return errors.New("invalid space identifier")
	}
	mutexName, err := windows.UTF16PtrFromString(driveMutexPrefix + *profileID + "-" + *spaceFlag)
	if err != nil {
		return err
	}
	mutex, mutexErr := windows.CreateMutex(nil, false, mutexName)
	if errors.Is(mutexErr, windows.ERROR_ALREADY_EXISTS) {
		if mutex != 0 {
			windows.CloseHandle(mutex)
		}
		return errors.New("Cloud Storage Drive is already running")
	}
	if mutexErr != nil {
		return mutexErr
	}
	defer windows.CloseHandle(mutex)
	letter := strings.ToUpper(strings.TrimSuffix(*mountpoint, ":"))
	if !regexp.MustCompile(`^[D-Z]$`).MatchString(letter) || *dataDir == "" {
		return errors.New("invalid drive mount configuration")
	}
	statusPath := filepath.Join(*dataDir, "drives", *profileID+"-"+*spaceFlag+".json")
	baseStatus := statusDocument{ProfileID: *profileID, SpaceID: *spaceFlag, DriveLetter: letter}
	baseStatus.State = "starting"
	baseStatus.Detail = "Подключаемся к серверу"
	writeStatus(statusPath, baseStatus)
	p, token, remoteSession, err := loadProfile(*dataDir, *profileID)
	if err != nil {
		baseStatus.State, baseStatus.Detail = "error", err.Error()
		writeStatus(statusPath, baseStatus)
		return err
	}
	api, err := newAPIClient(p, token, remoteSession)
	if err != nil {
		baseStatus.State, baseStatus.Detail = "error", err.Error()
		writeStatus(statusPath, baseStatus)
		return err
	}
	spaceID := *spaceFlag
	if spaceID == "" {
		spaces, err := api.listSpaces()
		if err != nil || len(spaces) == 0 {
			if err == nil {
				err = errors.New("no personal space is available")
			}
			baseStatus.State, baseStatus.Detail = "offline", err.Error()
			writeStatus(statusPath, baseStatus)
			return err
		}
		spaceID = spaces[0].ID
	}
	cacheRoot := filepath.Join(*dataDir, "drive-cache", *profileID, spaceID)
	if err := os.MkdirAll(cacheRoot, 0o700); err != nil {
		return err
	}
	cloud := newCloudFileSystem(api, spaceID, cacheRoot)
	behaviour, err := gofs.NewOptions(
		cloud,
		gofs.WithCaseInsensitive(true),
		gofs.WithDefaultWinfspOptions(winfsp.FileSystemName("CloudStorage")),
	)
	if err != nil {
		return err
	}
	mounted, err := winfsp.Mount(behaviour, letter+":")
	if err != nil {
		baseStatus.State, baseStatus.Detail = "error", "WinFsp: "+err.Error()
		writeStatus(statusPath, baseStatus)
		return err
	}
	defer mounted.Unmount()
	rootPath, rootErr := windows.UTF16PtrFromString(letter + ":\\")
	volumeLabel, labelErr := windows.UTF16PtrFromString("Личный диск")
	if rootErr == nil && labelErr == nil {
		_ = windows.SetVolumeLabel(rootPath, volumeLabel)
	}
	baseStatus.State = "ready"
	baseStatus.Detail = "Ленивое скачивание и запись на сервер включены"
	writeStatus(statusPath, baseStatus)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			_ = os.Remove(statusPath)
			return nil
		case <-ticker.C:
			if !parentAlive(*parentPID) {
				_ = os.Remove(statusPath)
				return nil
			}
			writeStatus(statusPath, baseStatus)
		}
	}
}

func main() {
	if err := run(); err != nil {
		os.Exit(2)
	}
}
