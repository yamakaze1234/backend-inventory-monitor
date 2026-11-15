import test from 'node:test';
import assert from 'node:assert/strict';
import {isFixed,routeFixed,sendFixedReply} from '../integrations/order_backend/control/harness-route.mjs';
// 所有执行和投递均为内存替身，禁止真实消息。
process.env.STOCK_OPS_GROUP='group';process.env.STOCK_OPS_ORG='org';
const msg={text:'帮助',conversationType:'group',isInAtList:true,robotCode:'robot',conversationId:'group',senderCorpId:'org',senderStaffId:'user',sourceMsgId:'msg'};
test('固定指令和普通对话分流',()=>{assert.ok(isFixed('添加监控 88088 阈值 10'));assert.equal(isFixed('你好'),false);});
test('假@和其他群不进入执行器',async()=>{let calls=0;for(const change of [{isInAtList:false},{conversationId:'other'},{conversationType:'private'},{robotCode:'other'}])await routeFixed({...msg,...change},'robot',async()=>{calls++;},async()=>{calls++;});assert.equal(calls,0);});
test('单次执行投递并记回执',async()=>{const states=[];const execute=async(e,s)=>{states.push(s);return {Text:'fixture',Receipt:true};};await routeFixed(msg,'robot',async()=>true,execute);assert.deepEqual(states,['prepare','sent']);});
test('投递异常只记未知不重试',async()=>{const states=[];await routeFixed(msg,'robot',async()=>{throw Error('timeout');},async(e,s)=>{states.push(s);return {Text:'fixture',Receipt:true};});assert.deepEqual(states,['prepare','unknown']);});
test('拒绝非钉钉地址发送',async()=>{assert.equal(await sendFixedReply({},'https://example.com/send','fixture','text'),false);});

 test("remove watch stays on fixed routing", () => { assert.equal(isFixed("删除监控 56281"), true); assert.equal(isFixed("删除监控 全部"), true); });
