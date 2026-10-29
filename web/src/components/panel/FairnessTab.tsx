// Fairness tab (design doc 10.3, US-12): nights and weekend shifts per nurse, the current period
// stacked on earlier published periods, with the ward average (regular nurses) as a line. One
// chart per measure (never two scales on one axis); one blue hue, lighter = earlier periods.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, type FairnessOut } from "../../api/client";
import QueryState from "../common/QueryState";

const EARLIER = "#86b6ef"; // blue 250 (validated ordinal pair, light end clears 2:1)
const CURRENT = "#1c5cab"; // blue 550
const INK = "#52514e";
const MUTED = "#898781";

type Measure = "nights" | "weekends";

function rows(f: FairnessOut, m: Measure) {
  return f.nurses
    .filter((n) => !n.standby)
    .map((n) => ({
      name: n.name.split(" ")[0],
      full: `${n.name} (${n.nurse_id})`,
      earlier: m === "nights" ? n.earlier_nights : n.earlier_weekends,
      current: m === "nights" ? n.nights : n.weekend_shifts,
    }));
}

function Chart({ f, m }: { f: FairnessOut; m: Measure }) {
  const data = rows(f, m);
  const avg = m === "nights" ? f.ward_average.all_nights : f.ward_average.all_weekends;
  return (
    <figure>
      <figcaption className="mb-1 text-xs font-semibold text-slate-700">
        {m === "nights" ? "Nights" : "Weekend shifts"} per nurse
        <span className="ml-1 font-normal text-slate-500">(ward average {avg})</span>
      </figcaption>
      <div style={{ height: Math.max(180, data.length * 18 + 40) }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} layout="vertical" margin={{ top: 4, right: 12, bottom: 4, left: 0 }} barCategoryGap={3}>
            <CartesianGrid horizontal={false} stroke="#ecebe8" />
            <XAxis type="number" allowDecimals={false} tick={{ fontSize: 10, fill: MUTED }} stroke={MUTED} />
            <YAxis type="category" dataKey="name" width={58} tick={{ fontSize: 10, fill: INK }} stroke={MUTED} interval={0} />
            <Tooltip
              cursor={{ fill: "#f4f3f0" }}
              contentStyle={{ fontSize: 11 }}
              labelFormatter={(_l, p) => (p?.[0]?.payload as { full?: string } | undefined)?.full ?? ""}
            />
            <Legend wrapperStyle={{ fontSize: 10, color: INK }} iconSize={8} />
            <Bar dataKey="earlier" name={`Earlier ${f.earlier_periods} periods`} stackId="a" fill={EARLIER} stroke="#fcfcfb" strokeWidth={1} />
            <Bar dataKey="current" name="This period" stackId="a" fill={CURRENT} stroke="#fcfcfb" strokeWidth={1} radius={[0, 4, 4, 0]} />
            <ReferenceLine x={avg} stroke={INK} strokeDasharray="4 3" label={{ value: "avg", position: "top", fontSize: 10, fill: INK }} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

export default function FairnessTab() {
  const f = useQuery({ queryKey: ["fairness"], queryFn: api.fairness });
  const [table, setTable] = useState(false);
  const data = f.data;
  return (
    <QueryState query={f} label="fairness">
      {data && (
        <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-3">
          <Chart f={data} m="nights" />
          <Chart f={data} m="weekends" />
      <button onClick={() => setTable((x) => !x)} className="text-[11px] text-indigo-600 hover:underline">
        {table ? "Hide table" : "Show as table"}
      </button>
      {table && (
        <table className="w-full text-[11px]">
          <thead className="text-left text-slate-500">
            <tr>
              <th>Nurse</th>
              <th>Nights (earlier + now)</th>
              <th>Weekends</th>
              <th>Denied</th>
            </tr>
          </thead>
          <tbody>
            {data.nurses.map((n) => (
              <tr key={n.nurse_id} className="border-t border-slate-100">
                <td>
                  {n.name}
                  {n.standby && " (standby)"}
                </td>
                <td>
                  {n.earlier_nights} + {n.nights}
                </td>
                <td>
                  {n.earlier_weekends} + {n.weekend_shifts}
                </td>
                <td>
                  {n.earlier_denied} + {n.requests_denied}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
        </div>
      )}
    </QueryState>
  );
}
