/*
 * ai-diagnosis-plugin: 智能诊断
 *
 * Adds a "智能诊断" section to the detail page of diagnosable resources
 * (Pod, Deployment, Node, PVC), and a Diagnosis Center in the sidebar with a
 * dedicated detail page and an unread app-bar badge.
 */

import {
  registerAppBarAction,
  registerDetailsViewSection,
  registerRoute,
  registerSidebarEntry,
} from '@kinvolk/headlamp-plugin/lib';
import { DetailsViewSectionProps } from '@kinvolk/headlamp-plugin/lib';
import React from 'react';
import DiagnosisCenter from './DiagnosisCenter';
import DiagnosisDetail from './DiagnosisDetail';
import DiagnosisNotificationsBadge from './DiagnosisNotificationsBadge';
import DiagnosisSection from './DiagnosisSection';
import ErrorBoundary from './ErrorBoundary';
import { ROUTE_CENTER, ROUTE_DETAIL } from './routes';

const DIAGNOSABLE_KINDS = ['Pod', 'Deployment', 'Node', 'PersistentVolumeClaim'];

registerSidebarEntry({
  parent: null,
  name: ROUTE_CENTER,
  label: '智能诊断',
  url: `/${ROUTE_CENTER}`,
  icon: 'mdi:stethoscope',
});

registerRoute({
  path: `/${ROUTE_CENTER}`,
  sidebar: ROUTE_CENTER,
  name: ROUTE_CENTER,
  exact: true,
  component: () => (
    <ErrorBoundary>
      <DiagnosisCenter />
    </ErrorBoundary>
  ),
});

registerRoute({
  path: `/${ROUTE_CENTER}/:diagnosisId`,
  sidebar: ROUTE_CENTER,
  name: ROUTE_DETAIL,
  exact: true,
  component: () => (
    <ErrorBoundary>
      <DiagnosisDetail />
    </ErrorBoundary>
  ),
});

registerAppBarAction(DiagnosisNotificationsBadge);

registerDetailsViewSection(({ resource }: DetailsViewSectionProps) => {
  if (!resource || !DIAGNOSABLE_KINDS.includes(resource.kind)) {
    return null;
  }
  return <DiagnosisSection resource={resource} />;
});
