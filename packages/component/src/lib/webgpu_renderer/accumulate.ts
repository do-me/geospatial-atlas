// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import type { Dataflow, Node } from "../dataflow.js";
import type { BindGroups } from "./bind_groups.js";
import type { AuxiliaryResources, DataBuffers } from "./renderer.js";
import { accumulatePipelineConstants, resolveWgConfig } from "./wg_config.js";

export function makeAccumulateCommand(
  df: Dataflow,
  device: Node<GPUDevice>,
  module: Node<GPUShaderModule>,
  bindGroups: BindGroups,
  dataBuffers: DataBuffers,
  auxiliaryResources: AuxiliaryResources,
): Node<(encoder: GPUCommandEncoder) => void> {
  const wgConfig = resolveWgConfig();
  const constants = accumulatePipelineConstants(wgConfig);
  // Host-side dispatch keeps step with shader's `id.y * ACCUMULATE_STRIDE + id.x`.
  const workgroupsX = Math.max(1, Math.floor(wgConfig.accumulateStride / wgConfig.accumulate));
  let pipeline = df.derive([device, module, bindGroups.layouts], (device, module, layouts) =>
    device.createComputePipeline({
      layout: device.createPipelineLayout({ bindGroupLayouts: [layouts.group0, layouts.group1, layouts.group2A] }),
      compute: { module: module, entryPoint: "accumulate", constants },
    }),
  );
  return df.derive(
    [
      pipeline,
      bindGroups.group0,
      bindGroups.group1,
      bindGroups.group2A,
      auxiliaryResources.countBuffer,
      dataBuffers.count,
    ],
    (pipeline, group0, group1, group2A, countBuffer, count) => (encoder) => {
      encoder.clearBuffer(countBuffer);
      if (count == 0) {
        return;
      }
      let pass = encoder.beginComputePass();
      pass.setPipeline(pipeline);
      pass.setBindGroup(0, group0);
      pass.setBindGroup(1, group1);
      pass.setBindGroup(2, group2A);
      const threadsPerRow = wgConfig.accumulateStride;
      if (count <= threadsPerRow) {
        pass.dispatchWorkgroups(Math.ceil(count / wgConfig.accumulate));
      } else {
        pass.dispatchWorkgroups(workgroupsX, Math.ceil(count / threadsPerRow));
      }
      pass.end();
    },
  );
}
