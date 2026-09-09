package datasource

import "strings"

func stringsTrimRightSlash(s string) string {
	return strings.TrimRight(s, "/")
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
