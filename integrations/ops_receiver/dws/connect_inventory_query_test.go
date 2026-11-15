package helpers

import (
	"encoding/json"
	"github.com/open-dingtalk/dingtalk-stream-sdk-go/chatbot"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestInventoryPilotRejectsUnverifiedQueries(t *testing.T) {
	p := inventoryQueryPilot{"robot", "org"}
	var data chatbot.BotCallbackDataModel
	if err := json.Unmarshal([]byte(`{"robotCode":"robot","senderCorpId":"org","conversationId":"cid-group","senderStaffId":"member","msgId":"evt-1","conversationType":"2","msgtype":"text","isInAtList":true,"text":{"content":"查库存 56281"}}`), &data); err != nil {
		t.Fatal(err)
	}
	got, ok := p.event(&data)
	if !ok || got.Message != "evt-1" || got.Text != "查库存 56281" {
		t.Fatalf("有效事件未被识别: %#v", got)
	}
	for name, change := range map[string]func(*chatbot.BotCallbackDataModel){
		"未@":       func(d *chatbot.BotCallbackDataModel) { d.IsInAtList = false },
		"跨组织":      func(d *chatbot.BotCallbackDataModel) { d.SenderCorpId = "other" },
		"缺少组织成员身份": func(d *chatbot.BotCallbackDataModel) { d.SenderStaffId = "" },

		"无有效群":   func(d *chatbot.BotCallbackDataModel) { d.ConversationId = "other" },
		"非固定写入":  func(d *chatbot.BotCallbackDataModel) { d.Text.Content = "顺便修改一下阈值" },
		"没有消息编号": func(d *chatbot.BotCallbackDataModel) { d.MsgId = "" },
	} {
		copy := data
		change(&copy)
		if _, ok := p.event(&copy); ok {
			t.Errorf("%s 未拒绝", name)
		}
	}
}

func TestInventoryPilotRoutesNaturalInventoryWithoutExpandingScope(t *testing.T) {
	for input, want := range map[string]string{
		"查询商品 5070 Ti 雪鹰":           "查询商品 5070 Ti 雪鹰",
		"查库存 5070 Ti 雪鹰":            "查库存 5070 Ti 雪鹰",
		"查询5070ti 雪鹰 库存和erp价格":      "查询商品 5070ti 雪鹰",
		"查询 5070 Ti 雪鹰的库存和 ERP 价格？": "查询商品 5070 Ti 雪鹰",
		"帮我查一下 5070 Ti 雪鹰 ERP 成本价":  "查询商品 5070 Ti 雪鹰",
		"查询５６２８１库存":                 "查询商品 56281",
		"修改监控阈值 56281 为 10":         "修改监控阈值 56281 为 10",
		"查价格变化 56281":               "查价格变化 56281",
	} {
		if got, ok := canonicalInventoryQuery(input); !ok || got != want {
			t.Errorf("输入 %q 得到 %q, %v；期望 %q", input, got, ok, want)
		}
	}
	for _, input := range []string{"查询订单进度", "查询库存", "查询 5070 Ti；修改阈值库存", "查询 5070 Ti 库存然后调整价格"} {
		if got, ok := canonicalInventoryQuery(input); ok {
			t.Errorf("无效查询被路由：%q -> %q", input, got)
		}
	}
}

// 第二个同组织成员由工作台授权表决定；拒绝的事件不能接管原机器人回复。
func TestInventoryBridgeRequiresGrantedMember(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/commands/v1/member" || r.Header.Get("X-Actor-Org-Id") != "org" ||
			r.Header.Get("X-Actor-Group-Id") != "cid-other" || r.Header.Get("X-Actor-User-Id") != "second" {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		_, _ = w.Write([]byte(`{"member":true}`))
	}))
	defer api.Close()
	bridge := inventoryQueryBridge{url: api.URL}
	event := inventoryQueryEvent{Org: "org", Group: "cid-other", User: "second"}
	if !bridge.member(event) {
		t.Fatal("已授权第二名成员未识别")
	}
	event.User = "unknown"
	if bridge.member(event) {
		t.Fatal("未授权成员被接管")
	}
}
