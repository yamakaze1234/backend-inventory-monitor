package helpers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// 进程初始化后立即从环境移走指令凭证，避免传到原有 Codex 子进程。
var inventoryPilotAuth = os.Getenv("INVENTORY_QUERY_AUTH")

func init() { _ = os.Unsetenv("INVENTORY_QUERY_AUTH") }

type inventoryQueryBridge struct {
	pilot                      inventoryQueryPilot
	python, script, url, group string
}

type inventoryQueryReply struct {
	Text    string
	Receipt string
}

func configuredInventoryQueryBridge(robot string) inventoryQueryBridge {
	return inventoryQueryBridge{
		pilot:  inventoryQueryPilot{robot, os.Getenv("INVENTORY_QUERY_ORG_ID")},
		python: os.Getenv("INVENTORY_QUERY_PYTHON"), script: os.Getenv("INVENTORY_QUERY_SCRIPT"), url: os.Getenv("INVENTORY_QUERY_URL"),
		group: os.Getenv("INVENTORY_QUERY_GROUP_ID"),
	}
}

func (b inventoryQueryBridge) ready() bool {
	u, err := url.Parse(b.url)
	return len(inventoryPilotAuth) >= 32 && filepath.IsAbs(b.python) && filepath.IsAbs(b.script) &&
		err == nil && u.Scheme == "http" && u.Hostname() == "127.0.0.1" && u.Port() != "" &&
		u.User == nil && u.Path == "" && u.RawQuery == "" && u.Fragment == ""
}

// 在拦截前向本机 ACL 查询；未知、未绑定或未授权的消息仍走原接收流程。
var inventoryMemberClient = &http.Client{
	Timeout:       900 * time.Millisecond,
	Transport:     &http.Transport{}, // 本地凭据不经系统代理。
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

func (b inventoryQueryBridge) member(event inventoryQueryEvent) bool {
	request, err := http.NewRequest(http.MethodGet, b.url+"/api/commands/v1/member", nil)
	if err != nil {
		return false
	}
	request.Header.Set("Authorization", "Bearer "+inventoryPilotAuth)
	request.Header.Set("X-Actor-Org-Id", event.Org)
	request.Header.Set("X-Actor-Group-Id", event.Group)
	request.Header.Set("X-Actor-User-Id", event.User)
	response, err := inventoryMemberClient.Do(request)
	if err != nil {
		return false
	}
	defer response.Body.Close()
	var result struct{ Member bool }
	return response.StatusCode == http.StatusOK && json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&result) == nil && result.Member
}

// 固定的新版订单指令必须经过 ACL；授权失败也不能回到旧代理执行。
func (b inventoryQueryBridge) ownsOrderCommand(event inventoryQueryEvent) bool {
	if b.group == "" || event.Group != b.group {
		return false
	}
	word, _, _ := strings.Cut(event.Text, " ")
	switch word {
	case "匹配订单", "巡检订单", "查看任务进度", "查订单状态":
		return true
	}
	return false
}

func (b inventoryQueryBridge) query(event inventoryQueryEvent, state string) (inventoryQueryReply, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 55*time.Second)
	defer cancel()
	request, err := json.Marshal(struct {
		Event inventoryQueryEvent
		State string
	}{event, state})
	if err != nil {
		return inventoryQueryReply{}, err
	}
	cmd := exec.CommandContext(ctx, b.python, "-B", "-X", "utf8", b.script)
	cmd.Env = append(os.Environ(), "INVENTORY_QUERY_AUTH="+inventoryPilotAuth, "INVENTORY_QUERY_URL="+b.url)
	cmd.Stdin = bytes.NewReader(request)
	out, err := cmd.Output()
	if err != nil {
		return inventoryQueryReply{}, err
	}
	var result inventoryQueryReply
	err = json.Unmarshal(out, &result)
	return result, err
}

// 同一钉钉会话只投递一次；200 还须核对业务回执，未知时不重发。
func sendInventoryQueryReply(endpoint, reply string) error {
	u, err := url.Parse(endpoint)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Hostname() == "" {
		return errors.New("回复地址无效")
	}
	body, err := json.Marshal(map[string]any{"msgtype": "text", "text": map[string]string{"content": reply}})
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	client := http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	res, err := client.Do(req)
	if err != nil {
		return errors.New("回复未确认")
	}
	defer res.Body.Close()
	var ack map[string]json.RawMessage
	if res.StatusCode != http.StatusOK || json.NewDecoder(io.LimitReader(res.Body, 65536)).Decode(&ack) != nil || string(ack["errcode"]) != "0" {
		return errors.New("回复业务回执未确认")
	}
	return nil
}
