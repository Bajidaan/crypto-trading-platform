import React from 'react';
import { ListOrdered } from 'lucide-react';
import type { OrderItem } from '../types';

interface OrdersTableProps {
  orders: OrderItem[];
}

export const OrdersTable: React.FC<OrdersTableProps> = ({ orders }) => {
  return (
    <div className="bg-darkpanel border border-darkborder rounded-xl p-4 shadow-lg flex flex-col">
      <div className="flex items-center justify-between pb-3 mb-3 border-b border-darkborder">
        <div className="flex items-center space-x-2">
          <ListOrdered className="w-4 h-4 text-blue-400" />
          <h2 className="text-sm font-bold text-white uppercase tracking-wider">
            Order Lifecycle & Fills ({orders.length})
          </h2>
        </div>
        <span className="text-xs text-gray-400">Idempotent Client Order IDs</span>
      </div>

      {orders.length === 0 ? (
        <div className="py-6 text-center text-gray-500 text-xs">
          No orders placed yet.
        </div>
      ) : (
        <div className="overflow-x-auto max-h-60">
          <table className="w-full text-left text-xs font-mono">
            <thead className="sticky top-0 bg-darkpanel">
              <tr className="text-gray-400 border-b border-darkborder pb-2">
                <th className="py-2 px-3 font-medium">Time / Client ID</th>
                <th className="py-2 px-3 font-medium">Symbol</th>
                <th className="py-2 px-3 font-medium">Purpose</th>
                <th className="py-2 px-3 font-medium">Side / Type</th>
                <th className="py-2 px-3 font-medium">Qty (Fill / Req)</th>
                <th className="py-2 px-3 font-medium">Avg Price</th>
                <th className="py-2 px-3 font-medium text-right">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-darkborder/50">
              {orders.map((o) => {
                const isBuy = o.side === 'BUY';
                const isFilled = o.status === 'FILLED';

                return (
                  <tr key={o.id} className="hover:bg-darkcard/50 transition">
                    <td className="py-2 px-3">
                      <div className="text-gray-400 text-[10px]">
                        {new Date(o.created_at).toLocaleTimeString()}
                      </div>
                      <div className="text-gray-300 text-[10px] truncate max-w-[140px]" title={o.client_order_id}>
                        {o.client_order_id}
                      </div>
                    </td>

                    <td className="py-2 px-3">
                      <div className="flex items-center space-x-1.5">
                        <span className="font-bold text-white">{o.symbol}</span>
                        <span
                          className={`text-[9px] font-mono px-1.5 py-0.2 rounded font-bold uppercase ${
                            (o.strategy_abbr || '').includes('DCA')
                              ? 'bg-purple-500/20 text-purple-300 border border-purple-500/30'
                              : (o.strategy_abbr || '').includes('EMA')
                              ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30'
                              : 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
                          }`}
                          title={o.strategy_name || 'Strategy'}
                        >
                          {o.strategy_abbr || 'STRAT'}
                        </span>
                      </div>
                      <div className="text-[10px] text-gray-400 truncate max-w-[120px]" title={o.strategy_name || ''}>
                        {o.strategy_name || ''}
                      </div>
                    </td>

                    <td className="py-2 px-3">
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${
                          o.purpose === 'ENTRY'
                            ? 'bg-blue-500/20 text-blue-300'
                            : o.purpose.includes('SL')
                            ? 'bg-rose-500/20 text-rose-300'
                            : 'bg-emerald-500/20 text-emerald-300'
                        }`}
                      >
                        {o.purpose}
                      </span>
                    </td>

                    <td className="py-2 px-3">
                      <span
                        className={`font-bold ${
                          isBuy ? 'text-emerald-400' : 'text-rose-400'
                        }`}
                      >
                        {o.side}
                      </span>{' '}
                      <span className="text-gray-400 text-[10px]">{o.order_type}</span>
                    </td>

                    <td className="py-2 px-3 text-gray-200">
                      {o.filled_qty} / {o.requested_qty}
                    </td>

                    <td className="py-2 px-3 text-white">
                      {o.avg_fill_price != null
                        ? `$${Number(o.avg_fill_price).toLocaleString(undefined, { minimumFractionDigits: 2 })}`
                        : o.limit_price != null
                        ? `$${Number(o.limit_price).toLocaleString(undefined, { minimumFractionDigits: 2 })} (LMT)`
                        : 'MKT'}
                    </td>

                    <td className="py-2 px-3 text-right">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-[10px] font-bold ${
                          isFilled
                            ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                            : o.status === 'CANCELED'
                            ? 'bg-gray-800 text-gray-400 border border-darkborder'
                            : 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                        }`}
                      >
                        {o.status}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};
