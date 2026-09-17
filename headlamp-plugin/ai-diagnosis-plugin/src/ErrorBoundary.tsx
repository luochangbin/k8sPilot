/** Minimal error boundary so a render crash is visible, not a blank page. */

import { Alert } from '@mui/material';
import React from 'react';

interface Props {
  children?: React.ReactNode;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <Alert severity="error" sx={{ m: 2 }}>
          智能诊断页面渲染失败：{this.state.error.message}
        </Alert>
      );
    }
    return this.props.children;
  }
}
