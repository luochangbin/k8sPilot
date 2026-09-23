import { SectionBox } from '@kinvolk/headlamp-plugin/lib/CommonComponents';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  Link as MuiLink,
  List,
  ListItem,
  ListItemText,
  Stack,
  Typography,
} from '@mui/material';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import InvestigationSteps from './InvestigationSteps';
import { centerUrl } from './routes';

/**
 * Agent Service base URL.
 *
 * Override at runtime by defining window.__K8S_PILOT_AGENT_BASE__ before the
 * app loads (e.g. via Headlamp config), or edit this constant.
 */
const AGENT_BASE_URL =
  (window as unknown as { __K8S_PILOT_AGENT_BASE__?: string }).__K8S_PILOT_AGENT_BASE__ ??
  'http://localhost:8001';

const POLL_INTERVAL_MS = 2000;

interface Evidence {
  source: string;
  observed_at?: string | null;
  summary: string;
}

interface Citation {
  document_id?: string;
  title?: string;
  source_uri?: string;
  section?: string;
  version?: string;
  updated_at?: string;
}

interface KnowledgeReference {
  retrieval_id: string;
  type?: string;
  score?: number;
  content: string;
  citation?: Citation;
  used_for?: string;
}

interface HistoricalCase {
  retrieval_id: string;
  type?: string;
  score?: number;
  incident_id?: string;
  product?: string;
  product_version?: string;
  resource_kind?: string;
  symptoms?: string[];
  root_cause_code?: string;
  evidence_summary?: string;
  remediation_summary?: string;
  verification?: {
    outcome?: string;
    verified_at?: string;
  };
  used_for?: string;
}

interface DiagnosisResult {
  symptom: string;
  evidence: Evidence[];
  root_cause?: string | null;
  confidence?: string | null;
  recommendations: string[];
  missing_evidence: string[];
  investigation_steps: string[];
  historical_cases?: HistoricalCase[];
  knowledge_references?: KnowledgeReference[];
}

interface Diagnosis {
  diagnosis_id: string;
  status: 'queued' | 'investigating' | 'completed' | 'failed';
  result?: DiagnosisResult | null;
  error?: string | null;
}

interface ResourceLike {
  kind: string;
  cluster: string;
  apiVersion?: string;
  metadata?: {
    name?: string;
    namespace?: string;
    uid?: string;
  };
}

/** Fallback mapping when Headlamp does not hand us the resource's apiVersion. */
function apiVersionForKind(kind: string): string {
  switch (kind) {
    case 'Deployment':
    case 'ReplicaSet':
    case 'StatefulSet':
    case 'DaemonSet':
      return 'apps/v1';
    case 'Job':
    case 'CronJob':
      return 'batch/v1';
    default:
      return 'v1';
  }
}

async function createDiagnosis(resource: ResourceLike): Promise<{ diagnosis_id: string }> {
  const res = await fetch(`${AGENT_BASE_URL}/api/v1/diagnoses`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      trigger: 'manual',
      resource: {
        apiVersion: resource.apiVersion ?? apiVersionForKind(resource.kind),
        kind: resource.kind,
        namespace: resource.metadata?.namespace,
        name: resource.metadata?.name,
        uid: resource.metadata?.uid,
      },
    }),
  });
  if (!res.ok) {
    throw new Error(`创建诊断失败 (HTTP ${res.status}): ${await res.text()}`);
  }
  return res.json();
}

async function fetchDiagnosis(diagnosisId: string): Promise<Diagnosis> {
  const res = await fetch(`${AGENT_BASE_URL}/api/v1/diagnoses/${diagnosisId}`);
  if (!res.ok) {
    throw new Error(`查询诊断失败 (HTTP ${res.status}): ${await res.text()}`);
  }
  return res.json();
}

function confidenceColor(confidence?: string | null) {
  switch (confidence) {
    case 'high':
      return 'success';
    case 'medium':
      return 'warning';
    case 'low':
      return 'error';
    default:
      return 'default';
  }
}

export default function DiagnosisSection({ resource }: { resource: ResourceLike }) {
  const [diagnosis, setDiagnosis] = useState<Diagnosis | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);
  const inFlightRef = useRef(false);

  const stopPolling = useCallback(() => {
    stoppedRef.current = true;
    if (pollTimer.current) {
      clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const startDiagnosis = useCallback(async () => {
    setError(null);
    setDiagnosis(null);
    setRunning(true);
    stopPolling(); // reset any previous run
    stoppedRef.current = false;
    inFlightRef.current = false;
    try {
      const created = await createDiagnosis(resource);
      // Recursive setTimeout + in-flight guard: a terminal first poll never
      // schedules another poll, and a slow request cannot overlap the next one.
      const tick = async (): Promise<void> => {
        if (stoppedRef.current || inFlightRef.current) return;
        inFlightRef.current = true;
        try {
          const current = await fetchDiagnosis(created.diagnosis_id);
          if (stoppedRef.current) return;
          setDiagnosis(current);
          if (current.status === 'completed' || current.status === 'failed') {
            stopPolling();
            setRunning(false);
            return;
          }
        } catch (e) {
          if (stoppedRef.current) return;
          stopPolling();
          setRunning(false);
          setError((e as Error).message);
          return;
        } finally {
          inFlightRef.current = false;
        }
        if (!stoppedRef.current) {
          pollTimer.current = setTimeout(() => {
            void tick();
          }, POLL_INTERVAL_MS);
        }
      };
      await tick();
    } catch (e) {
      setRunning(false);
      setError((e as Error).message);
    }
  }, [resource, stopPolling]);

  const name = resource.metadata?.name ?? '';

  return (
    <SectionBox title="智能诊断">
      {error && (
        <Alert severity="error" sx={{ mb: 1 }}>
          {error}
        </Alert>
      )}

      {!running && !diagnosis && (
        <Button variant="contained" onClick={startDiagnosis} disabled={!name}>
          智能诊断
        </Button>
      )}

      {running && (
        <Stack direction="row" spacing={1} alignItems="center">
          <CircularProgress size={20} />
          <Typography variant="body2">
            正在诊断 {name} ...（{diagnosis?.status ?? 'queued'}）
          </Typography>
        </Stack>
      )}

      {running && diagnosis?.result?.investigation_steps?.length ? (
        <InvestigationSteps steps={diagnosis.result.investigation_steps} />
      ) : null}

      {!running && diagnosis?.status === 'completed' && diagnosis.result && (
        <ResultView result={diagnosis.result} />
      )}

      {!running && diagnosis?.status === 'failed' && (
        <Alert severity="error">诊断失败：{diagnosis.error ?? '未知错误'}</Alert>
      )}

      {name && (
        <Box sx={{ mt: 2 }}>
          <MuiLink
            component={Link}
            to={centerUrl(
              resource.metadata?.uid
                ? `uid=${encodeURIComponent(resource.metadata.uid)}`
                : undefined
            )}
          >
            查看完整诊断
          </MuiLink>
        </Box>
      )}
    </SectionBox>
  );
}

const USED_FOR_LABELS: Record<string, string> = {
  hypothesis: '假设形成',
  investigation: '调查指导',
  explanation: '解释辅助',
  recommendation: '建议来源',
};

function usedForLabel(usedFor?: string): string | null {
  if (!usedFor) return null;
  return USED_FOR_LABELS[usedFor] ?? usedFor;
}

function EvidenceBlock({ evidence }: { evidence: Evidence[] }) {
  return (
    <Box
      sx={{
        borderLeft: 3,
        borderColor: 'primary.main',
        pl: 1.5,
        mb: 1,
      }}
    >
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
        <Typography variant="subtitle1" fontWeight={600}>
          关键证据
        </Typography>
        <Chip label="实时" color="primary" size="small" variant="outlined" />
      </Stack>
      <List dense disablePadding>
        {evidence.map((ev, idx) => (
          <ListItem key={`${ev.source}-${idx}`} disableGutters>
            <ListItemText
              primary={ev.summary}
              secondary={`来源: ${ev.source}${ev.observed_at ? ` · ${ev.observed_at}` : ''}`}
            />
          </ListItem>
        ))}
        {evidence.length === 0 && <ListItemText primary="（无）" />}
      </List>
    </Box>
  );
}

function KnowledgeRefBlock({ references }: { references: KnowledgeReference[] }) {
  if (references.length === 0) return null;
  return (
    <Box
      sx={{
        bgcolor: 'action.hover',
        borderRadius: 1,
        p: 1.5,
        mb: 1,
      }}
    >
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="subtitle1" fontWeight={600}>
          文档知识参考
        </Typography>
        <Chip label="参考" color="secondary" size="small" />
        <Typography variant="caption" color="text.secondary">
          仅辅助解释与建议，不构成实时证据
        </Typography>
      </Stack>
      {references.map(ref => {
        const citation = ref.citation ?? {};
        const used = usedForLabel(ref.used_for);
        return (
          <Box key={ref.retrieval_id} sx={{ mb: 1.5 }}>
            <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
              <Typography variant="body2" fontWeight={600}>
                {citation.title || '知识文档'}
              </Typography>
              {used && <Chip label={used} size="small" variant="outlined" />}
            </Stack>
            <Typography variant="body2" sx={{ my: 0.5 }}>
              {ref.content}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {[
                citation.document_id,
                citation.section && `§ ${citation.section}`,
                citation.version && `v${citation.version}`,
              ]
                .filter(Boolean)
                .join(' · ')}
              {citation.source_uri && (
                <>
                  {' · '}
                  <a
                    href={citation.source_uri}
                    target="_blank"
                    rel="noreferrer"
                    style={{ color: 'inherit' }}
                  >
                    来源文档
                  </a>
                </>
              )}
            </Typography>
          </Box>
        );
      })}
    </Box>
  );
}

function HistoricalCaseBlock({ cases }: { cases: HistoricalCase[] }) {
  if (cases.length === 0) return null;
  return (
    <Box
      sx={{
        bgcolor: 'action.hover',
        borderRadius: 1,
        p: 1.5,
        mb: 1,
      }}
    >
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="subtitle1" fontWeight={600}>
          历史相似案例
        </Typography>
        <Chip label="参考" color="secondary" size="small" />
        <Typography variant="caption" color="text.secondary">
          相似经验回顾，不构成实时证据
        </Typography>
      </Stack>
      {cases.map(c => {
        const used = usedForLabel(c.used_for);
        const meta: string[] = [];
        if (c.product) meta.push(c.product + (c.product_version ? ` ${c.product_version}` : ''));
        if (c.resource_kind) meta.push(c.resource_kind);
        if (c.root_cause_code) meta.push(c.root_cause_code);
        return (
          <Box key={c.retrieval_id} sx={{ mb: 1.5 }}>
            <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
              <Typography variant="body2" fontWeight={600}>
                {c.incident_id || '历史案例'}
              </Typography>
              {used && <Chip label={used} size="small" variant="outlined" />}
            </Stack>
            {meta.length > 0 && (
              <Stack direction="row" spacing={1} sx={{ my: 0.5 }} flexWrap="wrap">
                {meta.map(tag => (
                  <Chip key={tag} label={tag} size="small" variant="outlined" />
                ))}
              </Stack>
            )}
            {c.symptoms && c.symptoms.length > 0 && (
              <Typography variant="body2">症状: {c.symptoms.join('、')}</Typography>
            )}
            {c.evidence_summary && (
              <Typography variant="body2">证据摘要: {c.evidence_summary}</Typography>
            )}
            {c.remediation_summary && (
              <Typography variant="body2">处置经验: {c.remediation_summary}</Typography>
            )}
            {c.verification && (
              <Typography variant="body2">
                验证:{' '}
                {c.verification.outcome === 'success' ? '修复成功' : c.verification.outcome ?? ''}
                {c.verification.verified_at ? `（${c.verification.verified_at}）` : ''}
              </Typography>
            )}
          </Box>
        );
      })}
    </Box>
  );
}

function ResultView({ result }: { result: DiagnosisResult }) {
  const hasRootCause = !!result.root_cause;
  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="subtitle1" fontWeight={600}>
          当前症状
        </Typography>
        <Chip
          label={`置信度: ${result.confidence ?? 'unknown'}`}
          color={confidenceColor(result.confidence)}
          size="small"
        />
      </Stack>
      <Typography variant="body1" sx={{ mb: 1 }}>
        {result.symptom || '（无）'}
      </Typography>

      {result.investigation_steps.length > 0 && (
        <>
          <Divider sx={{ my: 1 }} />
          <Typography variant="subtitle1" fontWeight={600} sx={{ mb: 0.5 }}>
            调查过程
          </Typography>
          <InvestigationSteps steps={result.investigation_steps} />
        </>
      )}

      <Divider sx={{ my: 1 }} />
      <Typography variant="subtitle1" fontWeight={600}>
        Root Cause
      </Typography>
      {hasRootCause ? (
        <Typography variant="body1" sx={{ mb: 1 }}>
          {result.root_cause}
        </Typography>
      ) : (
        <Alert severity="warning" sx={{ mb: 1 }}>
          证据不足，无法确定唯一根因。
          {result.missing_evidence.length > 0 && (
            <span> 缺少证据：{result.missing_evidence.join('；')}</span>
          )}
        </Alert>
      )}

      <Divider sx={{ my: 1 }} />
      <EvidenceBlock evidence={result.evidence} />

      <Divider sx={{ my: 1 }} />
      <Typography variant="subtitle1" fontWeight={600} sx={{ mb: 0.5 }}>
        修复建议
      </Typography>
      <List dense disablePadding>
        {result.recommendations.map((r, idx) => (
          <ListItem key={idx} disableGutters>
            <ListItemText primary={r} />
          </ListItem>
        ))}
        {result.recommendations.length === 0 && <ListItemText primary="（无）" />}
      </List>

      <Divider sx={{ my: 1 }} />
      <HistoricalCaseBlock cases={result.historical_cases ?? []} />

      <Divider sx={{ my: 1 }} />
      <KnowledgeRefBlock references={result.knowledge_references ?? []} />
    </Box>
  );
}
