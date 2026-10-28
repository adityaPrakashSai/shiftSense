// Plain-language labels for hard-constraint names (rest_after_night:N05:d7 -> "rest after night").
const LABEL: Record<string, string> = {
  rest_after_night: "rest after night",
  quick_return: "too little rest between shifts",
  max_consecutive_days: "too many days in a row",
  max_consecutive_nights: "night block too long",
  max_nights: "night cap for the period",
  max_shifts: "contract maximum",
  max_hours: "hours limit",
  one_shift_per_day: "already on a shift",
  unavailable: "unavailable",
  leave: "on leave",
  night_qualified: "not night-qualified",
  night_exempt: "exempt from nights",
  standby_off_roster: "standby not allowed",
  frozen: "shift already fixed",
  forced: "fixed duty",
  no_feasible_roster: "no legal roster with this nurse",
  min_cover: "minimum cover",
  max_cover: "shift already full",
  fixed_duty: "fixed duty",
};

export function ruleLabels(rules: string[]): string[] {
  const out: string[] = [];
  for (const r of rules) {
    const head = r.split(":")[0];
    const label = LABEL[head] ?? head.replace(/_/g, " ");
    if (!out.includes(label)) out.push(label);
  }
  return out;
}
