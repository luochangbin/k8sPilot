/**
 * Headlamp route helpers.
 *
 * Headlamp routes are cluster-prefixed by default, so links must be built with
 * `Router.createRouteURL` instead of hard-coded absolute paths. It returns ''
 * when the route is unknown and '/' when no cluster path param is present, so
 * both cases fall back to the plugin's own path and are logged for diagnosis.
 */

import { Router } from '@kinvolk/headlamp-plugin/lib';

export const ROUTE_CENTER = 'ai-diagnosis';
export const ROUTE_DETAIL = 'ai-diagnosis-detail';

function build(routeName: string, params?: Record<string, string>): string {
  let url = '';
  try {
    url = Router.createRouteURL(routeName, params);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(`[ai-diagnosis] createRouteURL(${routeName}) failed`, err);
    url = '';
  }
  if (!url || url === '/') {
    // Keep navigation usable (and observable) instead of silently landing home.
    // eslint-disable-next-line no-console
    console.warn(
      `[ai-diagnosis] createRouteURL(${routeName}) returned ${JSON.stringify(url)}; ` +
        'falling back to the plugin route path'
    );
    return params?.diagnosisId ? `/${ROUTE_CENTER}/${params.diagnosisId}` : `/${ROUTE_CENTER}`;
  }
  return url;
}

export function centerUrl(query?: string): string {
  const base = build(ROUTE_CENTER);
  return query ? `${base}?${query}` : base;
}

export function detailUrl(diagnosisId: string): string {
  return build(ROUTE_DETAIL, { diagnosisId });
}
