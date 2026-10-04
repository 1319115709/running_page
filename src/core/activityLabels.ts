export function activityLabel(type: string, locale: string): string {
  const labels: Record<string, string> = {
    Run: '跑步',
    Walk: '步行',
    WeightTraining: '力量训练',
    Workout: '综合训练',
    Ride: '骑行',
    Swim: '游泳',
    Hike: '徒步',
    Velomobile: '骑行',
  };
  return locale === 'zh' ? (labels[type] ?? type) : type;
}

export function activityIcon(type: string): string {
  const icons: Record<string, string> = {
    Run: '🏃',
    Walk: '🚶',
    WeightTraining: '🏋️',
    Workout: '💪',
    Ride: '🚴',
    Swim: '🏊',
    Hike: '🥾',
    Velomobile: '🚴',
  };
  return icons[type] ?? '📌';
}
