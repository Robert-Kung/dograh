'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';

import logger from '@/lib/logger';

import type { AuthUser, LocalUser } from '../types';
import { AuthContext } from './AuthProvider';

export function LocalProviderWrapper({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<LocalUser | null>(null);
  const [loading, setLoading] = useState(true);
  const tokenRef = useRef<string | null>(null);

  useEffect(() => {
    if (typeof window === 'undefined') return;

    const initializeAuth = async () => {
      try {
        const response = await fetch('/api/auth/oss');
        if (response.ok) {
          const data = await response.json();
          tokenRef.current = data.token;
          setUser(data.user);
          logger.info('OSS auth initialized', { user: data.user });
        } else if (response.status === 401) {
          // No token - redirect to login (but not if already on auth pages)
          if (!window.location.pathname.startsWith('/auth/')) {
            window.location.href = '/auth/login';
            return;
          }
        } else {
          logger.error('Failed to initialize OSS auth');
        }
      } catch (error) {
        logger.error('Error initializing OSS auth', error);
      } finally {
        setLoading(false);
      }
    };

    initializeAuth();
  }, []);

  const getAccessToken = React.useCallback(async () => {
    if (typeof window === 'undefined') {
      return 'ssr-placeholder-token';
    }
    if (!tokenRef.current) {
      logger.warn('No OSS token available after initialization');
      return '';
    }
    return tokenRef.current;
  }, []);

  const redirectToLogin = React.useCallback(() => {
    window.location.href = '/auth/login';
  }, []);

  // customer-center-platform fork（母 repo W2d 覆蓋面表的 raw fetch 軸）：
  //
  // 上游打 `POST /api/auth/logout`（dograh-ui 自己的 route），在本平台是
  // `default-deny` ⇒ 403 被 catch 吞掉，接著無條件跳 `/auth/login`，而閘門對
  // `/auth/*` 302 回 `/` ⇒ **閘門的 session 完全沒清**：一顆看起來登出、
  // 其實沒登出的按鈕，比沒有登出鈕更糟。
  //
  // 本平台的登出在閘門：`/__gateway/logout` 是一個**導航**目標（帶確認表單，
  // 且 CSRF 檢查要求同源導航），故這裡改為整頁導過去，不再發 XHR。
  const logout = React.useCallback(async () => {
    setUser(null);
    tokenRef.current = null;
    window.location.href = '/__gateway/logout';
  }, []);

  const contextValue = useMemo(() => ({
    user: user as AuthUser,
    isAuthenticated: !!user,
    loading,
    getAccessToken,
    redirectToLogin,
    logout,
    provider: 'local' as const,
  }), [user, loading, getAccessToken, redirectToLogin, logout]);

  return (
    <AuthContext.Provider value={contextValue}>
      {children}
    </AuthContext.Provider>
  );
}
