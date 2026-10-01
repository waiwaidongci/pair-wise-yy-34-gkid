from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; ID_PREFIX='OI'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed'])
MEASURE_ENTITY='共用整改措施'; MEASURE_PREFIX='M'
MEASURE_CREATE_ROLES=set(['investigator','safety_manager']); MEASURE_ATTACH_ROLES=set(['investigator','safety_manager']); MEASURE_ACCEPT_ROLES=set(['safety_manager']); MEASURE_VERIFY_ROLES=set(['safety_manager'])
LEGACY_ACTION_KINDS=set(['action','corrective_action','measure','整改措施','纠正措施']); MIGRATION_ACTOR='system:migration'; MIGRATION_BATCH='legacy-measures-v1'
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def _normalize_key(value):
    import re
    return re.sub(r"\s+","",str(value or "").strip()).lower()
def parse_legacy_identity(record):
    """从旧整改明细里解析工位和原因：优先解析detail中的JSON，否则按规范kind读取。"""
    import json
    kind=str(record.get("kind") or "").strip()
    detail=record.get("detail")
    workstation=""; reason=""
    if isinstance(detail,str) and detail[:1] in ("{","["):
        try:
            payload=json.loads(detail)
        except ValueError:
            payload=None
        if isinstance(payload,dict):
            workstation=payload.get("workstation") or payload.get("station") or payload.get("工位") or ""
            reason=payload.get("reason") or payload.get("cause") or payload.get("原因") or ""
            detail_text=payload.get("detail") or payload.get("description") or detail
        else:
            detail_text=detail
    else:
        detail_text=detail
    if not workstation and ":" in kind:
        workstation=kind.split(":",1)[1]
    if not reason:
        reason=str(detail_text or "").strip()
    workstation=str(workstation).strip() or "未注明工位"
    reason=str(reason).strip() or "未注明原因"
    return workstation,reason
def legacy_merge_key(record):
    workstation,reason=parse_legacy_identity(record)
    return _normalize_key(workstation)+"|"+_normalize_key(reason),workstation,reason
