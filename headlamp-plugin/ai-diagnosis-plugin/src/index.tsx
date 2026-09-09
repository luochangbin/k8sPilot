/*
 * ai-diagnosis-plugin: 智能诊断
 *
 * Adds a "智能诊断" section to the detail page of diagnosable resources
 * (Pod, Deployment, Node, PVC). Clicking the button sends a DiagnosisRequest
 * (full resource identity incl. uid) to the k8sPilot Agent Service, polls the
 * Diagnosis Session, and renders the structured result (symptom, investigation
 * steps, root cause, evidence, confidence, recommendations).
 */

import { registerDetailsViewSection } from '@kinvolk/headlamp-plugin/lib';
import { DetailsViewSectionProps } from '@kinvolk/headlamp-plugin/lib';
import DiagnosisSection from './DiagnosisSection';

const DIAGNOSABLE_KINDS = ['Pod', 'Deployment', 'Node', 'PersistentVolumeClaim'];

registerDetailsViewSection(({ resource }: DetailsViewSectionProps) => {
  if (!resource || !DIAGNOSABLE_KINDS.includes(resource.kind)) {
    return null;
  }
  return <DiagnosisSection resource={resource} />;
});
