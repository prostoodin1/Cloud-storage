//go:build windows

package main

import (
	"errors"
	"net/http"
	"net/url"
	"testing"
)

func TestNormalizeName(t *testing.T) {
	tests := map[string]string{
		`\Фото\Отпуск\море.jpg`: "Фото/Отпуск/море.jpg",
		"/":                     "",
		"Документы/report.txt":  "Документы/report.txt",
	}
	for input, expected := range tests {
		actual, err := normalizeName(input)
		if err != nil || actual != expected {
			t.Fatalf("normalizeName(%q) = %q, %v; want %q", input, actual, err, expected)
		}
	}
	if _, err := normalizeName("../secret"); err == nil {
		t.Fatal("path traversal was accepted")
	}
}

func TestRouteURLPreservesUnicodePathAndQuery(t *testing.T) {
	base, err := url.Parse("https://cloud.example:8766")
	if err != nil {
		t.Fatal(err)
	}
	client := &apiClient{baseURL: base}
	logical := "Фото/Лето 2026/море #1.jpg"
	target, err := client.routeURL(fileRoute("space-1", logical))
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(target)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Path != "/v1/spaces/space-1/files/Фото/Лето 2026/море #1.jpg" {
		t.Fatalf("unexpected decoded path: %q", parsed.Path)
	}
	if parsed.RawQuery != "" {
		t.Fatalf("unexpected query: %q", parsed.RawQuery)
	}

	listing, err := client.routeURL("/v1/spaces/space-1/entries?directory=" + url.QueryEscape("Фото/Лето 2026"))
	if err != nil {
		t.Fatal(err)
	}
	parsed, _ = url.Parse(listing)
	if parsed.Query().Get("directory") != "Фото/Лето 2026" {
		t.Fatalf("directory query was corrupted: %q", parsed.Query().Get("directory"))
	}
}

func TestAPIRouteRejectsAbsoluteURL(t *testing.T) {
	base, _ := url.Parse("https://cloud.example")
	client := &apiClient{baseURL: base}
	if _, err := client.routeURL("https://attacker.example/file"); err == nil {
		t.Fatal("absolute API route was accepted")
	}
}

func TestAPIClientDoesNotFollowRedirects(t *testing.T) {
	client, err := newAPIClient(
		profile{ServerURL: "https://cloud.example"},
		"token",
		"",
	)
	if err != nil {
		t.Fatal(err)
	}
	request, _ := http.NewRequest(http.MethodGet, "https://other.example/file", nil)
	if err := client.http.CheckRedirect(request, nil); !errors.Is(err, http.ErrUseLastResponse) {
		t.Fatalf("unexpected redirect policy: %v", err)
	}
}
