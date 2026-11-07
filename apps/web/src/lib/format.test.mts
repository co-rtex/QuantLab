// A value that rounds to zero is shown unsigned: "-0.00" read as a negative result that
// wasn't there (audit A10: a mean out-of-sample Sharpe of -0.0004). Run with Node's runner.
import assert from "node:assert/strict";
import { test } from "node:test";

type Mod = typeof import("./format");
const { num, pct, DASH } = (await import(new URL("./format.ts", import.meta.url).href)) as Mod;

test("num never prints a negative zero", () => {
  assert.equal(num(-0.0004), "0.00");
  assert.equal(num(-0.004, 2), "0.00");
  assert.equal(num(-0), "0.00");
  assert.equal(num(-0.005, 2), "-0.01"); // rounds away from zero: a real, visible negative
  assert.equal(num(-1.234, 1), "-1.2");
  assert.equal(num(1234.5, 1), "1,234.5");
  assert.equal(num(null), DASH);
  assert.equal(num(Number.NaN), DASH);
});

test("pct never prints a negative zero, and signs only what is positive after rounding", () => {
  assert.equal(pct(-0.00001, 1), "0.0%");
  assert.equal(pct(-0.0004, 2), "-0.04%");
  assert.equal(pct(0.00001, 1, true), "0.0%"); // not "+0.0%"
  assert.equal(pct(0.0123, 1, true), "+1.2%");
  assert.equal(pct(-0.0123, 1, true), "-1.2%");
  assert.equal(pct(undefined), DASH);
});
