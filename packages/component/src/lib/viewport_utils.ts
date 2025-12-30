// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import { type Matrix3 } from "./matrix.js";
import type { Point, ViewportState } from "./utils.js";

export class Viewport {
  private viewport: ViewportState;
  private width: number;
  private height: number;
  private isGis: boolean;

  private _matrix: Matrix3 = [0, 0, 0, 0, 0, 0, 0, 0, 0];
  private _pixel_kx: number = 0;
  private _pixel_bx: number = 0;
  private _pixel_ky: number = 0;
  private _pixel_by: number = 0;

  constructor(viewport: ViewportState, width: number, height: number, isGis: boolean = false) {
    this.viewport = viewport;
    this.width = width;
    this.height = height;
    this.isGis = isGis;
    this.updateCoefficients();
  }

  static projectLat(lat: number): number {
    const latRad = (lat * Math.PI) / 180;
    return (Math.log(Math.tan(Math.PI / 4 + latRad / 2)) * 180) / Math.PI;
  }

  static unprojectLat(y: number): number {
    const yRad = (y * Math.PI) / 180;
    return (2 * Math.atan(Math.exp(yRad)) - Math.PI / 2) * (180 / Math.PI);
  }

  update(viewport: ViewportState, width: number, height: number, isGis?: boolean) {
    this.viewport = viewport;
    this.width = width;
    this.height = height;
    if (isGis !== undefined) {
      this.isGis = isGis;
    }
    this.updateCoefficients();
  }

  private updateCoefficients() {
    let { x, y, scale } = this.viewport;
    let sx = scale;
    let sy = scale;
    if (this.width < this.height) {
      sx *= this.height / this.width;
    } else {
      sy *= this.width / this.height;
    }
    this._matrix = [sx, 0, 0, 0, sy, 0, -x * sx, -y * sy, 1];
    this._pixel_kx = (this._matrix[0] * this.width) / 2;
    this._pixel_bx = ((this._matrix[6] + 1) * this.width) / 2;
    this._pixel_ky = (-this._matrix[4] * this.height) / 2;
    this._pixel_by = ((-this._matrix[7] + 1) * this.height) / 2;
  }

  matrix(): Matrix3 {
    return this._matrix;
  }

  scale(): number {
    return Math.abs(this._pixel_kx);
  }

  pixelLocation(x: number, y: number): Point {
    const py = this.isGis ? Viewport.projectLat(y) : y;
    return { x: x * this._pixel_kx + this._pixel_bx, y: py * this._pixel_ky + this._pixel_by };
  }

  coordinateAtPixel(px: number, py: number): Point {
    const x = (px - this._pixel_bx) / this._pixel_kx;
    const y = (py - this._pixel_by) / this._pixel_ky;
    return { x, y: this.isGis ? Viewport.unprojectLat(y) : y };
  }

  pixelLocationFunction(): (x: number, y: number) => Point {
    let kx = this._pixel_kx;
    let ky = this._pixel_ky;
    let bx = this._pixel_bx;
    let by = this._pixel_by;
    let isGis = this.isGis;
    return (x, y) => {
      const py = isGis ? Viewport.projectLat(y) : y;
      return { x: x * kx + bx, y: py * ky + by };
    };
  }

  coordinateAtPixelFunction(): (px: number, py: number) => Point {
    let kx = this._pixel_kx;
    let ky = this._pixel_ky;
    let bx = this._pixel_bx;
    let by = this._pixel_by;
    let isGis = this.isGis;
    return (px, py) => {
      const x = (px - bx) / kx;
      const y = (py - by) / ky;
      return { x, y: isGis ? Viewport.unprojectLat(y) : y };
    };
  }
}
