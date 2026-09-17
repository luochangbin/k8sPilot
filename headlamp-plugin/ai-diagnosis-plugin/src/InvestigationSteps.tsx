/** Shared checklist renderer for investigation steps (resource page + center detail). */

import { List, ListItem, ListItemIcon, ListItemText } from '@mui/material';
import React from 'react';

function StepCheckIcon() {
  return (
    <svg
      data-testid="step-check"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M5 12l5 5 9-10" />
    </svg>
  );
}

export default function InvestigationSteps({ steps }: { steps: string[] }) {
  return (
    <List dense>
      {steps.map(step => (
        <ListItem key={step} disableGutters>
          <ListItemIcon sx={{ minWidth: 28, color: 'success.main' }}>
            <StepCheckIcon />
          </ListItemIcon>
          <ListItemText primary={step} />
        </ListItem>
      ))}
    </List>
  );
}
