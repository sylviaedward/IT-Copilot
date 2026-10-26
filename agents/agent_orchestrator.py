"""IT-Copilot multi-agent runtime with real AsyncAnthropic tool-use loops."""
from __future__ import annotations
import asyncio, inspect, json, logging, os, time, uuid
from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from anthropic import AsyncAnthropic
from agents.tools import AgentToolSpec, build_shared_rag_tools, triage_tools, endpoint_tools, access_tools, network_software_tools, ticket_tools, escalation_tools
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from core.llm_utils import extract_text_content
logger=logging.getLogger(__name__)

def _env_float(name, default):
    try: return float(os.getenv(name,str(default)))
    except (TypeError,ValueError): return default
def _env_int(name, default):
    try: return int(os.getenv(name,str(default)))
    except (TypeError,ValueError): return default

class AgentType(Enum):
    TRIAGE="triage"; ENDPOINT="endpoint"; ACCESS="access"; NETWORK_SOFTWARE="network_software"; TICKET="ticket"

@dataclass(frozen=True)
class AgentProfile:
    role:str; mission:str; workflow:Tuple[str,...]; input_contract:Tuple[str,...]; output_contract:Tuple[str,...]
    handoff_conditions:Tuple[str,...]=(); tool_scope:Tuple[str,...]=(); model:Optional[str]=None; temperature:float=.2; max_tokens:int=3000

@dataclass
class AgentStats:
    total:int=0; success:int=0; total_ms:float=0.; monitor_penalty:float=0.; samples:Any=field(default_factory=lambda:deque(maxlen=50)); retry_at:float=0.; probe_running:bool=False; probe_owner:Optional[str]=None; observer:Any=field(default=None,repr=False)
    def recent(self):
        cutoff=time.monotonic()-300
        while self.samples and self.samples[0][0]<cutoff: self.samples.popleft()
        n=len(self.samples); return n,(sum(x[1] for x in self.samples)/n if n else 1.),(sum(x[2] for x in self.samples)/n if n else 0.)
    def available(self): return not self.probe_running and (not self.retry_at or time.monotonic()>=self.retry_at)
    def set_penalty(self,p):
        self.monitor_penalty=max(0.,min(.9,float(p)))
        if self.monitor_penalty>=_env_float("IT_COPILOT_MONITOR_FALLBACK_PENALTY",.5) and not self.retry_at: self.retry_at=time.monotonic()+max(1,_env_float("IT_COPILOT_MONITOR_COOLDOWN_SECONDS",60))
    def record(self,success,ms,request_id=None):
        if self.probe_running and request_id==self.probe_owner:
            self.probe_running=False; self.probe_owner=None
            if success: self.samples.clear(); self.retry_at=0.; self.monitor_penalty=0.
            else: self.retry_at=time.monotonic()+max(1,_env_float("IT_COPILOT_MONITOR_COOLDOWN_SECONDS",60))
        self.samples.append((time.monotonic(),int(success),ms))
        if self.observer: self.observer(ms)
    @property
    def success_rate(self): return self.success/self.total if self.total else 1.
    @property
    def avg_ms(self): return self.total_ms/self.total if self.total else 0.
    def routing_score(self): return (self.success_rate*.7 + (1/(1+self.avg_ms/1000))*.3)*max(0.,1-self.monitor_penalty)

@dataclass
class Request:
    message:str; user_id:str="anonymous"; conv_id:str="default"; context:str=""; history:Optional[List[Dict[str,str]]]=None; entities:Dict[str,List[str]]=field(default_factory=dict); intent:Optional[IntentCategory]=None; intent_group:Optional[str]=None; urgency:Optional[UrgencyLevel]=None; intent_confidence:float=1.; department:Optional[str]=None; request_id:str=field(default_factory=lambda:str(uuid.uuid4())); tool_attempts:List[Dict[str,Any]]=field(default_factory=list); before_unsafe_tool:Any=field(default=None,repr=False); unsafe_tool_calls:set=field(default_factory=set,repr=False); conversation_state:Optional[Dict[str,Any]]=None

@dataclass
class AgentResponse:
    agent_type:AgentType; content:str; success:bool; confidence:float=1.; latency_ms:float=0.; escalate:bool=False; tools_used:List[str]=field(default_factory=list); tool_traces:List[Dict[str,Any]]=field(default_factory=list); status:str="success"; error_code:Optional[str]=None; token_usage:Dict[str,int]=field(default_factory=dict)
@dataclass
class AgentCallResult:
    content:str; tools_used:List[str]=field(default_factory=list); tool_traces:List[Dict[str,Any]]=field(default_factory=list); status:str="success"; token_usage:Dict[str,int]=field(default_factory=dict)
class AgentCallError(RuntimeError):
    def __init__(self,message,tools_used,tool_traces): super().__init__(message); self.tools_used=list(tools_used); self.tool_traces=list(tool_traces)
@dataclass
class RoutingDecision:
    primary_agent:AgentType; supporting_agents:List[AgentType]=field(default_factory=list); reason:str=""; confidence:float=0.
    @property
    def agent_types(self): return [self.primary_agent]+self.supporting_agents
    @property
    def multi_agent(self): return bool(self.supporting_agents)
@dataclass
class OrchestratorResult:
    request_id:str; response:str; agent_type:AgentType; intent:Optional[IntentCategory]; escalated:bool=False; latency_ms:float=0.; agent_types:List[AgentType]=field(default_factory=list); primary_agent:Optional[AgentType]=None; supporting_agents:List[AgentType]=field(default_factory=list); tools_used:List[str]=field(default_factory=list); tool_traces:List[Dict[str,Any]]=field(default_factory=list); routing_reason:str=""; routing_confidence:float=0.; status:str="success"; retryable:bool=False; error_code:Optional[str]=None; plan:Optional[Dict[str,Any]]=None; findings:List[Dict[str,Any]]=field(default_factory=list); trace_events:List[Dict[str,Any]]=field(default_factory=list); token_usage:Dict[str,int]=field(default_factory=dict)
    @property
    def trace_id(self): return self.request_id

class BaseAgent:
    system_prompt="你是企业 IT 服务台 Agent。"
    def __init__(self,client,model,skill_manager=None,profile=None): self._client=client; self.profile=profile or self.profile; self._model=self.profile.model or model; self._skill_manager=skill_manager; self.stats=AgentStats(); self._shared_tools={}
    def get_tools(self): return dict(self._shared_tools)
    def set_shared_tools(self,tools): self._shared_tools={n:s for n,s in (tools or {}).items() if s.allowed_agents is None or self.agent_type.value in s.allowed_agents}
    async def handle(self,req):
        t0=time.monotonic(); self.stats.total+=1
        if not self.stats.available(): return AgentResponse(self.agent_type,"当前 Agent 正在恢复探测，请稍后重试。",False,status="failed",error_code="agent_busy")
        try:
            result=await self._call_llm(req); ms=(time.monotonic()-t0)*1000; ok=result.status in {"success","partial"}; self.stats.success+=int(ok); self.stats.total_ms+=ms; self.stats.record(ok,ms,req.request_id)
            return AgentResponse(self.agent_type,result.content,ok,req.intent_confidence,ms,self._needs_escalation(result.content),result.tools_used,result.tool_traces,result.status,token_usage=result.token_usage)
        except asyncio.CancelledError: raise
        except Exception as ex:
            ms=(time.monotonic()-t0)*1000; self.stats.total_ms+=ms; self.stats.record(False,ms,req.request_id); logger.exception("%s LLM call failed",self.agent_type.value)
            return AgentResponse(self.agent_type,"抱歉，模型暂时无法处理该请求，请稍后重试。",False,latency_ms=ms,tools_used=getattr(ex,"tools_used",[]),tool_traces=getattr(ex,"tool_traces",[]),status="failed",error_code="agent_failed")
    async def _call_llm(self,req):
        messages=[]; token_usage={"input_tokens":0,"output_tokens":0}
        if req.context: messages += [{"role":"user","content":f"[背景信息]\n{req.context}"},{"role":"assistant","content":"我已了解背景信息。"}]
        if req.entities: messages += [{"role":"user","content":"[结构化实体]\n"+json.dumps(req.entities,ensure_ascii=False)},{"role":"assistant","content":"我会结合这些实体处理。"}]
        messages.append({"role":"user","content":req.message})
        tools=self.get_tools(); used=[]; traces=[]
        for _ in range(3):
            kwargs={"model":self._model,"max_tokens":self.profile.max_tokens,"temperature":self.profile.temperature,"system":self._build_system_prompt(req),"messages":messages}
            if tools: kwargs["tools"]=[{"name":s.name,"description":s.description,"input_schema":s.input_schema} for s in tools.values()]
            resp=await asyncio.wait_for(self._client.messages.create(**kwargs),timeout=max(1,_env_float("IT_COPILOT_LLM_TIMEOUT_S",30)))
            usage=getattr(resp,"usage",None)
            if usage is not None:
                token_usage["input_tokens"] += int(getattr(usage,"input_tokens",0) or 0)
                token_usage["output_tokens"] += int(getattr(usage,"output_tokens",0) or 0)
            blocks=list(getattr(resp,"content",[]) or []); calls=[b for b in blocks if self._block_type(b)=="tool_use"]
            if not calls:
                content=extract_text_content(blocks).strip()
                if not content: raise AgentCallError("模型未返回有效回复",used,traces)
                return AgentCallResult(content,used,traces,"partial" if getattr(resp,"stop_reason",None)=="max_tokens" else "success",token_usage)
            messages.append({"role":"assistant","content":blocks}); results=[]
            for block in calls:
                name=self._block_value(block,"name"); tool_id=self._block_value(block,"id"); args=self._block_value(block,"input") or {}; spec=tools.get(name); started=time.monotonic(); success=True; error=""
                try:
                    if spec is None: raise ValueError(f"工具不在 {self.agent_type.value} Agent 白名单中")
                    self._validate_tool_input(spec,args)
                    if not spec.retry_safe:
                        missing_slots=(req.conversation_state or {}).get("missing_fields") or []
                        if missing_slots:
                            raise ValueError("缺少必要信息，暂不执行写操作：" + ", ".join(missing_slots))
                        key=json.dumps([name,args],sort_keys=True,ensure_ascii=True)
                        if key in req.unsafe_tool_calls: raise ValueError("副作用工具不得在同一请求重复执行")
                        req.unsafe_tool_calls.add(key); req.tool_attempts.append({"tool_name":name,"retry_safe":False})
                        if req.before_unsafe_tool: await req.before_unsafe_tool(name)
                    result=spec.handler(req,args); result=await result if inspect.isawaitable(result) else result; used.append(name)
                except Exception as ex:
                    success=False; error=str(ex); result={"success":False,"error":error}; logger.warning("tool %s failed: %s",name,ex)
                traces.append({"agent_type":self.agent_type.value,"tool_name":name,"tool_use_id":tool_id,"input":args,"success":success,"latency_ms":round((time.monotonic()-started)*1000,1),"error":error,"retry_safe":spec.retry_safe if spec else True})
                results.append({"type":"tool_result","tool_use_id":tool_id,"content":json.dumps(result,ensure_ascii=False),"is_error":not success})
            messages.append({"role":"user","content":results})
        raise AgentCallError("工具调用超过最大轮数",used,traces)
    def _build_system_prompt(self,req):
        packet=f"\n角色：{self.profile.role}\n职责：{self.profile.mission}\n流程：{' -> '.join(self.profile.workflow)}\n输出：{'；'.join(self.profile.output_contract)}\n工具白名单：{', '.join(self.get_tools()) or '无'}\n安全边界：不得索取或回显密码、OTP、私钥；不得声称执行没有证据的变更。知识库回答必须基于工具返回的证据，并引用其中的 citation（文档标题和片段 ID）；没有证据时明确说明未检索到，不得编造来源。"
        prompt=self.system_prompt+packet
        missing=(req.conversation_state or {}).get("missing_fields") or []
        if missing:
            prompt += "\n当前缺少必填信息：" + ", ".join(missing) + "。先向用户澄清并收集这些字段，禁止调用任何写操作工具。"
        if self._skill_manager:
            skill=self._skill_manager.prompt_for(req.message,self.agent_type.value)
            if skill: prompt += "\n\n"+skill
        return prompt
    @staticmethod
    def _block_type(b): return b.get("type") if isinstance(b,dict) else getattr(b,"type",None)
    @staticmethod
    def _block_value(b,k): return b.get(k) if isinstance(b,dict) else getattr(b,k,None)
    @staticmethod
    def _validate_tool_input(spec,args):
        from jsonschema import validators, ValidationError
        try: validators.validator_for(spec.input_schema)(spec.input_schema).validate(args)
        except ValidationError as ex: raise ValueError(f"工具参数未通过 JSON Schema 校验（{ex.validator}）") from None
    @staticmethod
    def _needs_escalation(content): return any(x in content for x in ("转人工","人工客服","无法处理","escalate"))

class TriageAgent(BaseAgent):
    system_prompt="你是 IT-Copilot 的 Triage Agent，负责企业员工的首轮接待、自然聊天、问题澄清和安全分流。对‘你是谁’等普通问题直接自然回答；识别出 IT 问题时收集最少必要信息并给出下一步，不要虚构已执行操作。"
class EndpointAgent(BaseAgent): system_prompt="你是 IT-Copilot Endpoint Agent，负责设备资产、终端故障和低风险只读诊断。"
class AccessAgent(BaseAgent): system_prompt="你是 IT-Copilot Access Agent，负责密码、MFA、账号解锁和权限申请。高风险操作只生成待审批请求。"
class NetworkSoftwareAgent(BaseAgent): system_prompt="你是 IT-Copilot Network/Software Agent，负责 VPN、Wi-Fi、网络和软件安装排障，给出可验证、低风险步骤。"
class TicketAgent(BaseAgent): system_prompt="你是 IT-Copilot Ticket Agent，负责创建、查询和升级 IT 工单；没有执行证据不得声称工单或变更已完成。"

def profile(role,mission,tools): return AgentProfile(role,mission,("理解请求","收集最少必要信息","调用安全工具","给出可验证下一步"),("用户消息","意图","实体","记忆上下文"),("自然语言回答","证据和下一步","安全边界"),("需要审批或人工升级",),tuple(tools),max_tokens=3000)

class AgentOrchestrator:
    def __init__(self,api_key="",base_url=None,model="deepseek-chat",skill_manager=None,recognizer=None,rag_tool_manager=None,**kwargs):
        client=AsyncAnthropic(api_key=api_key,**({"base_url":base_url} if base_url else {})); self._client=client; self._model=model; self._skill_manager=skill_manager; self._intent_recognizer=recognizer or IntentRecognizer(api_key,base_url,model); self._shared_tools={}; self._recent_tool_traces=deque(maxlen=200)
        specs=[(AgentType.TRIAGE,TriageAgent,triage_tools(),profile("IT 分诊与首轮接待","回答普通问题、澄清需求并完成安全分流",triage_tools())),(AgentType.ENDPOINT,EndpointAgent,endpoint_tools(),profile("终端与设备支持","处理设备资产和终端故障",endpoint_tools())),(AgentType.ACCESS,AccessAgent,access_tools(),profile("身份与权限支持","处理密码、MFA、账号和最小权限流程",access_tools())),(AgentType.NETWORK_SOFTWARE,NetworkSoftwareAgent,network_software_tools(),profile("网络与软件支持","处理 VPN、Wi-Fi、网络及软件问题",network_software_tools())),(AgentType.TICKET,TicketAgent,ticket_tools(),profile("IT 工单服务","管理工单和人工升级",ticket_tools()))]
        self._pool={}
        for t,cls,local,prof in specs:
            agent=cls(client,model,skill_manager,profile=prof); agent.agent_type=t; self._pool[t]=[agent]
        shared=build_shared_rag_tools(rag_tool_manager) if rag_tool_manager else {}
        for t,_,local,_ in specs: self._pool[t][0].set_shared_tools({**shared,**local})
        self._shared_tools=shared
    def set_shared_tools(self,tools):
        self._shared_tools=dict(tools or {})
        for agents in self._pool.values():
            for agent in agents: agent.set_shared_tools({**self._shared_tools,**agent.get_tools()})
    def set_skill_manager(self,manager):
        self._skill_manager=manager
        for agents in self._pool.values():
            for agent in agents: agent._skill_manager=manager
    async def recognize_intent(self,message,history=None,previous_intent_group=None): return await self._intent_recognizer.recognize(message,history=history,previous_intent_group=previous_intent_group)
    def _best_agent(self,agent_type):
        agents=[a for a in self._pool.get(agent_type,[]) if a.stats.available()]
        return max(agents,key=lambda a:a.stats.routing_score()) if agents else (self._pool.get(agent_type) or [None])[0]
    def _route(self,intent):
        groups={IntentCategory.DEVICE_FAILURE:AgentType.ENDPOINT,IntentCategory.DEVICE_PROVISIONING:AgentType.ENDPOINT,IntentCategory.PASSWORD_RESET:AgentType.ACCESS,IntentCategory.ACCOUNT_UNLOCK:AgentType.ACCESS,IntentCategory.MFA_ISSUE:AgentType.ACCESS,IntentCategory.PERMISSION_REQUEST:AgentType.ACCESS,IntentCategory.ACCESS_REVIEW:AgentType.ACCESS,IntentCategory.VPN_ISSUE:AgentType.NETWORK_SOFTWARE,IntentCategory.WIFI_ISSUE:AgentType.NETWORK_SOFTWARE,IntentCategory.NETWORK_ISSUE:AgentType.NETWORK_SOFTWARE,IntentCategory.SOFTWARE_INSTALL:AgentType.NETWORK_SOFTWARE,IntentCategory.SOFTWARE_LICENSE:AgentType.NETWORK_SOFTWARE,IntentCategory.EMAIL_COLLAB_ISSUE:AgentType.NETWORK_SOFTWARE,IntentCategory.TICKET_CREATE:AgentType.TICKET,IntentCategory.TICKET_STATUS:AgentType.TICKET,IntentCategory.TICKET_CANCEL:AgentType.TICKET,IntentCategory.HUMAN_HANDOFF:AgentType.TICKET}
        return groups.get(intent,AgentType.TRIAGE)
    async def _execute(self,req,agent_type):
        agent=self._best_agent(agent_type) or self._best_agent(AgentType.TRIAGE)
        return await agent.handle(req) if agent else AgentResponse(AgentType.TRIAGE,"服务暂时不可用。",False,status="failed",error_code="agent_unavailable")
    async def run(self,req):
        started=time.monotonic()
        if req.intent is None:
            ir=await self._intent_recognizer.recognize(req.message,history=req.history); req.intent,req.intent_group,req.urgency,req.intent_confidence,req.entities=ir.intent,ir.intent_group,ir.urgency,ir.confidence,ir.entities
        from core.planner import build_plan, is_complex_request
        if is_complex_request(req.message):
            return await self._run_plan(req, build_plan(req.message, req.request_id))
        main=self._route(req.intent); supporting=[]
        if "和" in req.message and req.intent_group in {"network","access"}: supporting=[AgentType.TRIAGE]
        decision=RoutingDecision(main,supporting,f"intent={req.intent.value}; performance_score={self._best_agent(main).stats.routing_score():.3f}",req.intent_confidence)
        if decision.multi_agent: return await self.run_parallel(req,decision)
        response=await self._execute(req,main); result=OrchestratorResult(req.request_id,response.content,response.agent_type,req.intent,response.escalate or req.intent==IntentCategory.HUMAN_HANDOFF,(time.monotonic()-started)*1000,[response.agent_type],main,[],response.tools_used,response.tool_traces,decision.reason,decision.confidence,response.status,False,response.error_code,token_usage=response.token_usage); self._recent_tool_traces.append(result.tool_traces); return result

    async def _run_plan(self, req, plan):
        """Execute bounded dependency waves; independent steps run concurrently."""
        started=time.monotonic(); completed=set(); step_rows=[]; findings=[]; all_tools=[]; all_traces=[]; responses=[]
        while len(completed) < len(plan.steps) and len(step_rows) < plan.max_steps * 2:
            ready=[s for s in plan.steps if s.step_id not in completed and all(d in completed for d in s.depends_on)]
            if not ready:
                return OrchestratorResult(req.request_id,"计划依赖无法满足，已停止执行并转人工。",AgentType.TRIAGE,req.intent,escalated=True,latency_ms=(time.monotonic()-started)*1000,status="failed",error_code="plan_dependency_cycle",plan=plan.to_dict(),trace_events=step_rows)
            wave=ready[:3]
            step_rows.extend({"event":"step_started","step_id":s.step_id,"agent_type":s.agent_type,"status":"running"} for s in wave)
            from dataclasses import replace
            raw=await asyncio.gather(*[self._execute(replace(req,message=f"[计划步骤: {s.title}]\n{req.message}"),AgentType(s.agent_type)) for s in wave],return_exceptions=True)
            for step,response in zip(wave,raw):
                completed.add(step.step_id)
                if isinstance(response,Exception):
                    step_rows.append({"event":"step_finished","step_id":step.step_id,"status":"failed","error_code":"step_exception"}); continue
                responses.append(response); all_tools.extend(response.tools_used); all_traces.extend(response.tool_traces)
                findings.append({"step_id":step.step_id,"agent_type":response.agent_type.value,"content":response.content,"success":response.success,"evidence":response.tool_traces})
                step_rows.append({"event":"step_finished","step_id":step.step_id,"status":"success" if response.success else "failed","tools_used":response.tools_used,"latency_ms":round(response.latency_ms,1)})
        if not responses:
            return OrchestratorResult(req.request_id,"所有计划步骤均未成功，请转人工处理。",AgentType.TRIAGE,req.intent,escalated=True,latency_ms=(time.monotonic()-started)*1000,status="failed",error_code="plan_failed",plan=plan.to_dict(),findings=findings,trace_events=step_rows)
        primary=next((r for r in responses if r.agent_type==AgentType.TICKET),responses[0])
        content="\n\n".join(f"【{f['agent_type']}】\n{f['content']}" for f in findings)
        status="success" if all(r.success for r in responses) else "partial"
        token_usage={"input_tokens":sum(r.token_usage.get("input_tokens",0) for r in responses),"output_tokens":sum(r.token_usage.get("output_tokens",0) for r in responses)}
        return OrchestratorResult(req.request_id,content,primary.agent_type,req.intent,any(r.escalate for r in responses),(time.monotonic()-started)*1000,list(dict.fromkeys(r.agent_type for r in responses)),primary.agent_type,[r.agent_type for r in responses if r.agent_type!=primary.agent_type],list(dict.fromkeys(all_tools)),all_traces,"bounded planner; parallel independent steps",req.intent_confidence,status,status=="partial",None,plan.to_dict(),findings,step_rows,token_usage)
    async def run_parallel(self,req,decision):
        started=time.monotonic(); responses=await asyncio.gather(*[self._execute(req,t) for t in decision.agent_types],return_exceptions=True); valid=[r for r in responses if isinstance(r,AgentResponse)]; content="\n\n".join(r.content for r in valid); first=valid[0] if valid else AgentResponse(decision.primary_agent,"所有 Agent 均处理失败。",False,status="failed",error_code="agent_failed"); token_usage={"input_tokens":sum(r.token_usage.get("input_tokens",0) for r in valid),"output_tokens":sum(r.token_usage.get("output_tokens",0) for r in valid)}; result=OrchestratorResult(req.request_id,content,decision.primary_agent,req.intent,any(r.escalate for r in valid),(time.monotonic()-started)*1000,[r.agent_type for r in valid],decision.primary_agent,decision.supporting_agents,[x for r in valid for x in r.tools_used],[x for r in valid for x in r.tool_traces],decision.reason,decision.confidence,"success" if len(valid)==len(responses) else "partial",token_usage=token_usage); self._recent_tool_traces.append(result.tool_traces); return result
    def get_stats(self):
        out={}
        for t,agents in self._pool.items():
            for i,a in enumerate(agents):
                recent_total,recent_success,recent_ms=a.stats.recent()
                out[f"{t.value}_{i}"]={"total":a.stats.total,"success_rate":round(a.stats.success_rate,3),"avg_ms":round(a.stats.avg_ms,1),"recent_total":recent_total,"recent_success_rate":round(recent_success,3),"recent_avg_ms":round(recent_ms,1),"routing_score":round(a.stats.routing_score(),3),"monitor_penalty":a.stats.monitor_penalty,"model":a._model,"tool_scope":list(a.get_tools())}
        return out
    def update_routing_penalties(self,penalties):
        for t,agents in self._pool.items():
            for i,a in enumerate(agents): a.stats.set_penalty(penalties.get(f"{t.value}_{i}",0.))
