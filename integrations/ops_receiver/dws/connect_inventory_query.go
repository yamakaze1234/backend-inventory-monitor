package helpers

import (
	"regexp"
	"strings"

	"github.com/open-dingtalk/dingtalk-stream-sdk-go/chatbot"
	"golang.org/x/text/unicode/norm"
)

// 试点从已校验的 Stream 回调读取身份，群文本仅参与固定命令匹配。
type inventoryQueryPilot struct {
	Robot, Org string
}

type inventoryQueryEvent struct {
	Robot, Org, Group, User, Message, Text string
}

// 将自然问法规范成「查询商品 <关键字>」。保持与工作台只读语法一致。
const inventoryERPPrice = `ERP\s*(?:库存成本|成本价|价格|单价|成本)`

var inventoryNaturalPrefix = regexp.MustCompile(`^(?:查询|帮我查(?:询|一下)?|查一下|查)\s*`)
var inventoryNaturalQuery = regexp.MustCompile(`(?i)^(.+?)\s*(?:的\s*)?(?:库存(?:\s*(?:和|及|与|、|以及|/)\s*` + inventoryERPPrice + `)?|` + inventoryERPPrice + `)[?。]?$`)

func canonicalInventoryQuery(text string) (string, bool) {
	if len([]rune(text)) > 500 {
		return "", false
	}
	parts := strings.Fields(text)
	if len(parts) == 0 {
		return "", false
	}
	clean := strings.Join(parts, " ")
	switch parts[0] {
	case "帮助", "查询商品", "查库存", "查价格变化", "添加监控", "修改监控阈值",
		"查订单状态", "匹配订单", "巡检订单", "查看任务进度", "发货监控汇总", "前台库存监控":
		return clean, true
	}
	normalized := norm.NFKC.String(clean)
	prefix := inventoryNaturalPrefix.FindStringIndex(normalized)
	if prefix == nil {
		return "", false
	}
	groups := inventoryNaturalQuery.FindStringSubmatch(normalized[prefix[1]:])
	if len(groups) != 2 {
		return "", false
	}
	keyword := strings.TrimSpace(groups[1])
	if keyword == "" || strings.ContainsAny(keyword, ";。?!") {
		return "", false
	}
	return "查询商品 " + keyword, true
}

func (p inventoryQueryPilot) event(d *chatbot.BotCallbackDataModel) (inventoryQueryEvent, bool) {
	if p.Robot == "" || p.Org == "" || d == nil ||
		d.ConversationType != "2" || !strings.HasPrefix(d.ConversationId, "cid") || d.Msgtype != "text" ||
		d.SenderCorpId != p.Org || strings.TrimSpace(d.SenderStaffId) == "" || !d.IsInAtList ||
		d.RobotCode != p.Robot || strings.TrimSpace(d.MsgId) == "" {
		return inventoryQueryEvent{}, false
	}
	text, ok := canonicalInventoryQuery(d.Text.Content)
	if !ok {
		return inventoryQueryEvent{}, false
	}
	return inventoryQueryEvent{p.Robot, d.SenderCorpId, d.ConversationId, d.SenderStaffId, d.MsgId, text}, true
}
