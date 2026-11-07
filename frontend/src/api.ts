export type Product = { id:number; sku:string; name:string; category:string; unit_cost_cents:number; stock_on_hand:number; reorder_point:number; lead_time_days:number };
export type Overview = { days:number; orders:number; units:number; revenue_cents:number; gross_margin_cents:number; average_order_cents:number; revenue_change_pct:number|null; inventory_value_cents:number; below_reorder:number; daily:{day:string;orders:number;units:number;revenue_cents:number;rolling_7d_cents:number}[]; categories:{category:string;units:number;revenue_cents:number}[]; top_products:{sku:string;name:string;units:number;revenue_cents:number;rank:number}[] };
export type Forecast = { sku:string;name:string;category:string;stock_on_hand:number;reorder_point:number;lead_time_days:number;predicted_units_7d:number;daily_forecast:number[];method:string;validation_mae_units:number|null;days_of_cover:number|null;reorder_suggested:boolean;suggested_restock_units:number };
export type Alerts = { inventory:{sku:string;name:string;stock_on_hand:number;days_of_cover:number|null;suggested_restock_units:number}[];sales:{day:string;kind:string;revenue_cents:number;baseline_cents:number;score:number}[] };
export type Order = {order_id:string;ordered_on:string;channel:string;lines:number;units:number;total_cents:number};
export type Movement = {id:number;delta:number;reason:string;reference:string;created_at:string};
export async function request<T>(path:string, init?:RequestInit):Promise<T> {
  const response=await fetch(`/api${path}`, { ...init, headers:{...(init?.body?{'Content-Type':'application/json'}:{}),...init?.headers} });
  if (!response.ok) { const data=await response.json().catch(()=>null); throw new Error(typeof data?.detail==='string'?data.detail:`Request failed (${response.status})`); }
  return response.json() as Promise<T>;
}
export const money=(cents:number)=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:0}).format(cents/100);
export const shortDate=(value:string)=>new Date(`${value.slice(0,10)}T12:00:00`).toLocaleDateString('en-US',{month:'short',day:'numeric'});
