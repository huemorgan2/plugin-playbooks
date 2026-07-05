/**
 * Trigger node — parallelogram shape, slate color.
 * Sits at the top of the graph.
 */

import { memo, useEffect, useState } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { Zap, Clock } from 'lucide-react'
import { cn } from '../../lib/cn'

interface TriggerNodeData {
  event?: string
  cron?: string
  label: string
  glowSeq?: number
  fireSeq?: number
  [key: string]: unknown
}

function TriggerNodeComponent({ data, selected }: NodeProps) {
  const d = data as unknown as TriggerNodeData
  const isCron = !!d.cron

  // 006.709: glow-and-fade on live add (slate, matching the trigger color).
  const [glowing, setGlowing] = useState(false)
  useEffect(() => {
    if (!d.glowSeq) return
    setGlowing(true)
    const t = setTimeout(() => setGlowing(false), 1600)
    return () => clearTimeout(t)
  }, [d.glowSeq])

  // 007.009.01: run-shimmer when the trigger fires during a replay.
  const [firing, setFiring] = useState(false)
  useEffect(() => {
    if (!d.fireSeq) return
    setFiring(true)
    const t = setTimeout(() => setFiring(false), 1500)
    return () => clearTimeout(t)
  }, [d.fireSeq])

  return (
    <>
      <div
        style={{ ['--glow-rgb' as string]: '148 163 184' }}
        className={cn(
          'relative px-5 py-2.5 min-w-[180px]',
          selected && 'ring-2 ring-luna-400/50 ring-offset-1 ring-offset-ink-950',
          glowing && 'node-arriving',
          firing && 'node-firing',
        )}
      >
        {/* Parallelogram background */}
        <div
          className="absolute inset-0 bg-slate-900/70 border border-slate-500/40 backdrop-blur-sm"
          style={{ transform: 'skewX(-8deg)', borderRadius: '8px' }}
        />
        <div className="relative flex items-center gap-2 z-10">
          <div className="w-6 h-6 rounded-md bg-slate-800/60 flex items-center justify-center shrink-0">
            {isCron ? <Clock className="w-3.5 h-3.5 text-slate-300" /> : <Zap className="w-3.5 h-3.5 text-slate-300" />}
          </div>
          <div className="min-w-0">
            <div className="text-xs font-medium text-slate-200 truncate">{d.label}</div>
            <div className="text-[10px] text-slate-500">trigger</div>
          </div>
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-slate-500 !w-2 !h-2 !border-slate-400" />
    </>
  )
}

export const TriggerNode = memo(TriggerNodeComponent)
