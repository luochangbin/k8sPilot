/** App-bar unread badge: navigates to the current cluster's diagnosis center. */

import { Icon } from '@iconify/react';
import { Utils } from '@kinvolk/headlamp-plugin/lib';
import { Badge, Box, IconButton, Tooltip } from '@mui/material';
import React from 'react';
import { Link } from 'react-router-dom';
import { centerUrl } from './routes';
import { useDiagnosisNotifications } from './useDiagnosisNotifications';

export default function DiagnosisNotificationsBadge() {
  const { unreadCount, liveMessage, ready } = useDiagnosisNotifications();
  // The center route is cluster-scoped: on the home page (no cluster selected)
  // navigating would 404, so the action is disabled until a cluster is chosen.
  const cluster = Utils.getCluster();
  const href = centerUrl();
  const badge = (
    <Badge color="error" badgeContent={unreadCount || undefined} max={99}>
      <Icon icon="mdi:stethoscope" />
    </Badge>
  );
  return (
    <Box sx={{ display: 'flex', alignItems: 'center' }}>
      <Tooltip title={cluster ? '智能诊断中心' : '请先选择集群'}>
        <span>
          {cluster ? (
            <IconButton
              component={Link}
              to={href}
              size="small"
              aria-label="打开智能诊断中心"
              data-testid="diagnosis-center-link"
              data-href={href}
            >
              {badge}
            </IconButton>
          ) : (
            <IconButton
              size="small"
              disabled
              aria-label="请先选择集群后打开智能诊断中心"
              data-testid="diagnosis-center-link"
            >
              {badge}
            </IconButton>
          )}
        </span>
      </Tooltip>
      <Box
        component="span"
        role="status"
        aria-live="polite"
        sx={{
          position: 'absolute',
          width: 1,
          height: 1,
          overflow: 'hidden',
          clip: 'rect(0 0 0 0)',
          whiteSpace: 'nowrap',
        }}
      >
        {ready ? liveMessage : ''}
      </Box>
    </Box>
  );
}
