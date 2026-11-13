/* Tests for when.js (the `when` condition builder). Run with:
 *   node editor_static/when_builder.test.js
 * Plain asserts, no libraries.
 */
"use strict";

var assert = require("assert");
var path = require("path");
var WhenBuilder = require(path.join(__dirname, "when.js"));
var whenToTemplate = WhenBuilder.whenToTemplate;
var templateToWhen = WhenBuilder.templateToWhen;

var tests = [];
function test(name, fn) { tests.push([name, fn]); }

// ---- whenToTemplate -------------------------------------------------------

test("is / is not", function () {
  assert.strictEqual(
    whenToTemplate({combine: "all", conditions: [{entity: "binary_sensor.door", op: "is", value: "on"}], time: null}),
    "{{ is_state('binary_sensor.door', 'on') }}");
  assert.strictEqual(
    whenToTemplate({combine: "all", conditions: [{entity: "binary_sensor.door", op: "is not", value: "on"}], time: null}),
    "{{ not is_state('binary_sensor.door', 'on') }}");
});

test("above / below / between", function () {
  assert.strictEqual(
    whenToTemplate({combine: "all", conditions: [{entity: "sensor.temp", op: "above", value: 20}], time: null}),
    "{{ (states('sensor.temp') | float(0) > 20) }}");
  assert.strictEqual(
    whenToTemplate({combine: "all", conditions: [{entity: "sensor.temp", op: "below", value: "5.5"}], time: null}),
    "{{ (states('sensor.temp') | float(0) < 5.5) }}");
  assert.strictEqual(
    whenToTemplate({combine: "all", conditions: [{entity: "sensor.temp", op: "between", value: 18, value2: 24}], time: null}),
    "{{ (states('sensor.temp') | float(0) >= 18 and states('sensor.temp') | float(0) <= 24) }}");
});

test("all / any combine", function () {
  var spec = {combine: "all", conditions: [
    {entity: "binary_sensor.door", op: "is", value: "on"},
    {entity: "sensor.temp", op: "above", value: 20},
  ], time: null};
  assert.strictEqual(whenToTemplate(spec),
    "{{ (is_state('binary_sensor.door', 'on') and (states('sensor.temp') | float(0) > 20)) }}");
  spec.combine = "any";
  assert.strictEqual(whenToTemplate(spec),
    "{{ (is_state('binary_sensor.door', 'on') or (states('sensor.temp') | float(0) > 20)) }}");
});

test("time window same day", function () {
  assert.strictEqual(
    whenToTemplate({combine: "all", conditions: [], time: {from: "08:00", to: "22:00"}}),
    "{{ (now().strftime('%H:%M') >= '08:00' and now().strftime('%H:%M') < '22:00') }}");
});

test("time window across midnight", function () {
  assert.strictEqual(
    whenToTemplate({combine: "all", conditions: [], time: {from: "22:00", to: "06:00"}}),
    "{{ (now().strftime('%H:%M') >= '22:00' or now().strftime('%H:%M') < '06:00') }}");
});

test("conditions plus time window", function () {
  var t = whenToTemplate({combine: "all", conditions: [
    {entity: "binary_sensor.door", op: "is", value: "on"}], time: {from: "08:00", to: "22:00"}});
  assert.strictEqual(t,
    "{{ (is_state('binary_sensor.door', 'on') and " +
    "(now().strftime('%H:%M') >= '08:00' and now().strftime('%H:%M') < '22:00')) }}");
});

test("empty spec", function () {
  assert.strictEqual(whenToTemplate({combine: "all", conditions: [], time: null}), "{{ true }}");
});

test("bad input throws", function () {
  assert.throws(function () { whenToTemplate({conditions: [{entity: "no dot", op: "is", value: "on"}]}); });
  assert.throws(function () { whenToTemplate({conditions: [{entity: "sensor.x", op: "above", value: "abc"}]}); });
  assert.throws(function () { whenToTemplate({conditions: [], time: {from: "25:00", to: "06:00"}}); });
  assert.throws(function () { whenToTemplate({conditions: [{entity: "sensor.x", op: "wat", value: 1}]}); });
});

// ---- templateToWhen -------------------------------------------------------

test("round-trip single conditions", function () {
  var specs = [
    {combine: "all", conditions: [{entity: "binary_sensor.door", op: "is", value: "on"}], time: null},
    {combine: "all", conditions: [{entity: "binary_sensor.door", op: "is not", value: "off"}], time: null},
    {combine: "all", conditions: [{entity: "sensor.temp", op: "above", value: 20}], time: null},
    {combine: "all", conditions: [{entity: "sensor.temp", op: "below", value: 5.5}], time: null},
    {combine: "all", conditions: [{entity: "sensor.temp", op: "between", value: 18, value2: 24}], time: null},
  ];
  specs.forEach(function (spec) {
    var back = templateToWhen(whenToTemplate(spec));
    assert.deepStrictEqual(back, spec, JSON.stringify(spec));
  });
});

test("round-trip all/any with several conditions", function () {
  var spec = {combine: "any", conditions: [
    {entity: "binary_sensor.door", op: "is", value: "on"},
    {entity: "sensor.temp", op: "above", value: 20},
    {entity: "sensor.hum", op: "below", value: 40},
  ], time: null};
  assert.deepStrictEqual(templateToWhen(whenToTemplate(spec)), spec);
  spec.combine = "all";
  assert.deepStrictEqual(templateToWhen(whenToTemplate(spec)), spec);
});

test("round-trip time windows, same day and across midnight", function () {
  var spec = {combine: "all", conditions: [{entity: "sensor.x", op: "above", value: 1}],
              time: {from: "08:00", to: "22:00"}};
  assert.deepStrictEqual(templateToWhen(whenToTemplate(spec)), spec);
  spec.time = {from: "22:00", to: "06:00"};
  assert.deepStrictEqual(templateToWhen(whenToTemplate(spec)), spec);
  spec.conditions = [];
  assert.deepStrictEqual(templateToWhen(whenToTemplate(spec)), spec);
});

test("round-trip empty spec", function () {
  assert.deepStrictEqual(templateToWhen("{{ true }}"),
                         {combine: "all", conditions: [], time: null});
});

test("hand-written / other templates -> null", function () {
  assert.strictEqual(templateToWhen("{{ states('sensor.x') | float(0) > 20 }}"), null);
  assert.strictEqual(templateToWhen("{{ is_state('binary_sensor.door', 'on') and states('sensor.y') == 'x' }}"), null);
  assert.strictEqual(templateToWhen("{{ now().hour >= 8 }}"), null);
  assert.strictEqual(templateToWhen("not a template"), null);
  assert.strictEqual(templateToWhen(""), null);
  assert.strictEqual(templateToWhen(null), null);
  assert.strictEqual(templateToWhen("{{ is_state('Bad Entity', 'on') }}"), null);
  assert.strictEqual(templateToWhen("{{ (states('sensor.a') | float(0) > 1 and states('sensor.b') | float(0) > 2) or is_state('c.d', 'on') }}"), null);
});

test("whitespace around the template is tolerated", function () {
  var spec = {combine: "all", conditions: [{entity: "binary_sensor.door", op: "is", value: "on"}], time: null};
  assert.deepStrictEqual(templateToWhen("  " + whenToTemplate(spec) + "\n"), spec);
});

// ---- run ------------------------------------------------------------------

var failed = 0;
tests.forEach(function (pair) {
  try {
    pair[1]();
    console.log("ok - " + pair[0]);
  } catch (err) {
    failed++;
    console.error("FAIL - " + pair[0] + "\n  " + (err && err.message));
  }
});
console.log(tests.length - failed + "/" + tests.length + " passed");
process.exit(failed ? 1 : 0);
