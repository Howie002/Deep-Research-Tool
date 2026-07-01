'use client';

import { useEffect, useRef } from 'react';
import * as d3 from 'd3';

export interface MMNode { id: string; label: string; kind: 'root' | 'search' | 'source' | 'note' | 'thought' }
export interface MMLink { source: string; target: string }

const KIND_COLOR: Record<string, string> = {
  root: '#500000', search: '#818cf8', source: '#38bdf8', note: '#34d399', thought: '#c084fc',
};

/**
 * Live research mind map — a D3 force-directed graph built from the run's
 * accumulated nodes (root query → searches → sources, plus notes and thought
 * nodes). Rebuilds when the node/link set changes. Captures the original
 * live-mindmap intent (search/fetch/note/thought growth) in a portable form.
 */
export default function MindMap({ nodes, links }: { nodes: MMNode[]; links: MMLink[] }) {
  const ref = useRef<SVGSVGElement>(null);

  useEffect(() => {
    const svg = d3.select(ref.current);
    svg.selectAll('*').remove();
    if (!ref.current || nodes.length === 0) return;
    const w = ref.current.clientWidth || 600;
    const h = 460;

    const n = nodes.map((d) => ({ ...d }));
    const l = links
      .filter((lk) => n.find((x) => x.id === lk.source) && n.find((x) => x.id === lk.target))
      .map((d) => ({ ...d }));

    const sim = d3.forceSimulation(n as d3.SimulationNodeDatum[])
      .force('link', d3.forceLink(l).id((d: d3.SimulationNodeDatum & { id?: string }) => d.id!).distance(70).strength(0.6))
      .force('charge', d3.forceManyBody().strength(-220))
      .force('center', d3.forceCenter(w / 2, h / 2))
      .force('collide', d3.forceCollide(26));

    const link = svg.append('g').attr('stroke', '#8884').attr('stroke-width', 1)
      .selectAll('line').data(l).join('line');

    const node = svg.append('g').selectAll<SVGGElement, (typeof n)[number]>('g').data(n).join('g')
      .call(d3.drag<SVGGElement, (typeof n)[number]>()
        .on('start', (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); (d as d3.SimulationNodeDatum).fx = (d as d3.SimulationNodeDatum).x; (d as d3.SimulationNodeDatum).fy = (d as d3.SimulationNodeDatum).y; })
        .on('drag', (e, d) => { (d as d3.SimulationNodeDatum).fx = e.x; (d as d3.SimulationNodeDatum).fy = e.y; })
        .on('end', (e, d) => { if (!e.active) sim.alphaTarget(0); (d as d3.SimulationNodeDatum).fx = null; (d as d3.SimulationNodeDatum).fy = null; }));

    node.append('circle')
      .attr('r', (d) => (d.kind === 'root' ? 14 : 7))
      .attr('fill', (d) => KIND_COLOR[d.kind] || '#888')
      .attr('stroke', '#fff2').attr('stroke-width', 1.5);

    node.append('text')
      .text((d) => (d.label.length > 28 ? d.label.slice(0, 28) + '…' : d.label))
      .attr('x', 10).attr('y', 4).attr('font-size', 10)
      .attr('fill', 'currentColor').attr('class', 'fill-zinc-600 dark:fill-zinc-300');

    node.append('title').text((d) => d.label);

    sim.on('tick', () => {
      link
        .attr('x1', (d) => (d.source as d3.SimulationNodeDatum).x!)
        .attr('y1', (d) => (d.source as d3.SimulationNodeDatum).y!)
        .attr('x2', (d) => (d.target as d3.SimulationNodeDatum).x!)
        .attr('y2', (d) => (d.target as d3.SimulationNodeDatum).y!);
      node.attr('transform', (d) => `translate(${(d as d3.SimulationNodeDatum).x},${(d as d3.SimulationNodeDatum).y})`);
    });

    return () => { sim.stop(); };
  }, [nodes, links]);

  return (
    <div className="w-full">
      {nodes.length === 0 ? (
        <p className="text-sm text-zinc-400 py-8 text-center">The mind map builds as the agent searches, reads, and takes notes.</p>
      ) : (
        <svg ref={ref} width="100%" height={460} className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900" />
      )}
    </div>
  );
}
