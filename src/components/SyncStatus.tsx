import { useState } from 'react';
import syncStatus from '@/static/sync_status.json';
import { useLocale } from '../hooks/useLocale';

interface SyncReport {
  checked_at: string | null;
  source_count: number;
  matched_count: number;
  missing_count: number;
  start_date: string;
  deduplicated_count?: number;
}

export function SyncStatus() {
  const { locale } = useLocale();
  const zh = locale === 'zh';
  const [now] = useState(() => Date.now());
  const report: SyncReport = syncStatus;
  const checked = report.checked_at ? new Date(report.checked_at) : null;
  const stale = checked ? now - checked.getTime() > 36 * 3600000 : false;
  const incomplete = report.missing_count > 0;
  return (
    <div className="mx-auto max-w-[1400px] px-4 pt-4 text-xs text-[var(--color-muted)] sm:px-6">
      <p>
        {zh
          ? '运动数据由 Garmin Forerunner 265 记录，通过 Intervals.icu 同步。'
          : 'Activities recorded with Garmin Forerunner 265 and synced via Intervals.icu.'}
      </p>
      <p className="mt-1" role="status">
        {checked ? (
          <>
            {zh ? '最近成功同步：' : 'Last successful sync: '}
            <time dateTime={report.checked_at ?? undefined}>
              {checked.toLocaleString(zh ? 'zh-CN' : 'en-GB', {
                timeZone: 'Asia/Shanghai',
                hour12: false,
              })}
            </time>
            {zh ? '（北京时间）' : ' (Beijing time)'} ·{' '}
            {zh
              ? `自 ${report.start_date} 起已核对 ${report.matched_count}/${report.source_count} 条活动`
              : `${report.matched_count}/${report.source_count} activities checked since ${report.start_date}`}
            {!!report.deduplicated_count && (
              <>
                {' · '}
                {zh
                  ? `${report.deduplicated_count} 条重复来源已归并`
                  : `${report.deduplicated_count} duplicate sources grouped`}
              </>
            )}
            {(stale || incomplete) && (
              <span className="ml-2 text-amber-600 dark:text-amber-400">
                {zh
                  ? incomplete
                    ? '部分活动尚未补齐'
                    : '同步延迟，请检查同步任务'
                  : incomplete
                    ? 'Some activities are missing'
                    : 'Sync delayed; check the sync job'}
              </span>
            )}
          </>
        ) : zh ? (
          '正在等待首次全量核对'
        ) : (
          'Waiting for the first complete reconciliation'
        )}
        {' · '}
        {zh ? '每日自动更新' : 'Updated daily'}
      </p>
    </div>
  );
}
