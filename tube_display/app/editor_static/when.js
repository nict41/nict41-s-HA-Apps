/* Condition builder for a screen's `when` field.
 *
 * A spec is {combine: "all"|"any", conditions: [...], time: {from, to}|null}:
 *   conditions: [{entity, op, value, value2}] with op one of
 *               "is" | "is not" | "above" | "below" | "between"
 *               (value2 only for "between")
 *   time:       optional {"from": "HH:MM", "to": "HH:MM"} window; from > to means
 *               the range crosses midnight (e.g. 22:00 -> 06:00)
 *
 * whenToTemplate(spec)  -> the Jinja template string for the `when` field
 * templateToWhen(text)  -> the spec back, or null when the template was not
 *                          written by the builder (hand-written, other syntax)
 *
 * Loaded by index.html before editor.js; also loadable in Node for the tests:
 *   node editor_static/when_builder.test.js
 */
(function (global) {
  "use strict";

  var ENTITY_RE = /^[a-z_]+\.[a-z0-9_]+$/;
  var TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

  function _q(s) {                       // a single-quoted Python/Jinja string literal
    return "'" + String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";
  }
  function _unq(s) {                     // the string inside such a literal
    return s.replace(/\\(.)/g, "$1");
  }
  function _num(v) {
    var n = Number(v);
    if (!isFinite(n)) throw new Error("not a number: " + v);
    return String(n);
  }

  function _cond(c) {
    if (!c || !ENTITY_RE.test(c.entity || "")) {
      throw new Error("bad entity id: " + (c && c.entity));
    }
    var e = _q(c.entity);
    switch (c.op) {
      case "is":
        return "is_state(" + e + ", " + _q(c.value == null ? "" : c.value) + ")";
      case "is not":
        return "not is_state(" + e + ", " + _q(c.value == null ? "" : c.value) + ")";
      case "above":
        return "(states(" + e + ") | float(0) > " + _num(c.value) + ")";
      case "below":
        return "(states(" + e + ") | float(0) < " + _num(c.value) + ")";
      case "between":
        return "(states(" + e + ") | float(0) >= " + _num(c.value) +
               " and states(" + e + ") | float(0) <= " + _num(c.value2) + ")";
    }
    throw new Error("bad op: " + (c && c.op));
  }

  function _time(t) {
    if (!t) return null;
    var from = t.from || "", to = t.to || "";
    if (!TIME_RE.test(from) || !TIME_RE.test(to)) throw new Error("bad time window");
    var now = "now().strftime('%H:%M')";
    if (from <= to) {                    // same day: from <= now < to
      return "(" + now + " >= " + _q(from) + " and " + now + " < " + _q(to) + ")";
    }
    return "(" + now + " >= " + _q(from) + " or " + now + " < " + _q(to) + ")";  // across midnight
  }

  function whenToTemplate(spec) {
    spec = spec || {};
    var parts = [];
    var conds = spec.conditions || [];
    for (var i = 0; i < conds.length; i++) parts.push(_cond(conds[i]));
    var t = _time(spec.time);
    if (t) parts.push(t);
    if (!parts.length) return "{{ true }}";
    var joiner = spec.combine === "any" ? " or " : " and ";
    var inner = parts.length > 1 ? "(" + parts.join(joiner) + ")" : parts[0];
    return "{{ " + inner + " }}";
  }

  // ---- parsing (the exact patterns whenToTemplate emits) -------------------

  var STR = "'((?:[^'\\\\]|\\\\.)*)'";   // a quoted literal, capture group 1 = contents
  var RE_IS = new RegExp("^is_state\\(" + STR + ", " + STR + "\\)$");
  var RE_NOT_IS = new RegExp("^not is_state\\(" + STR + ", " + STR + "\\)$");
  var NUM = "(-?\\d+(?:\\.\\d+)?)";
  var RE_CMP = new RegExp("^\\(states\\(" + STR + "\\) \\| float\\(0\\) (>|<) " + NUM + "\\)$");
  var RE_BETWEEN = new RegExp("^\\(states\\(" + STR + "\\) \\| float\\(0\\) >= " + NUM +
                              " and states\\(" + STR + "\\) \\| float\\(0\\) <= " + NUM + "\\)$");
  var NOW = "now\\(\\)\\.strftime\\('%H:%M'\\)";
  var RE_TIME_AND = new RegExp("^\\(" + NOW + " >= '([0-2]\\d:[0-5]\\d)' and " + NOW +
                               " < '([0-2]\\d:[0-5]\\d)'\\)$");
  var RE_TIME_OR = new RegExp("^\\(" + NOW + " >= '([0-2]\\d:[0-5]\\d)' or " + NOW +
                              " < '([0-2]\\d:[0-5]\\d)'\\)$");

  function _wraps(s) {                   // s = "(...)" with the first "(" matching the last ")"
    if (s.length < 2 || s[0] !== "(" || s[s.length - 1] !== ")") return false;
    var depth = 0;
    for (var i = 0; i < s.length; i++) {
      if (s[i] === "(") depth++;
      else if (s[i] === ")") {
        depth--;
        if (depth === 0 && i < s.length - 1) return false;
      }
    }
    return depth === 0;
  }

  function _splitTop(inner) {            // split on " and "/" or " outside parens and quotes
    var parts = [], cur = "", depth = 0, inQ = false, joiner = null, i = 0;
    while (i < inner.length) {
      var ch = inner[i];
      if (inQ) {
        cur += ch;
        if (ch === "\\" && i + 1 < inner.length) { cur += inner[i + 1]; i += 2; continue; }
        if (ch === "'") inQ = false;
        i++;
        continue;
      }
      if (depth === 0) {
        if (inner.startsWith(" and ", i)) {
          if (joiner === null) joiner = "and"; else if (joiner !== "and") return null;
          parts.push(cur); cur = ""; i += 5; continue;
        }
        if (inner.startsWith(" or ", i)) {
          if (joiner === null) joiner = "or"; else if (joiner !== "or") return null;
          parts.push(cur); cur = ""; i += 4; continue;
        }
      }
      if (ch === "'") inQ = true;
      else if (ch === "(") depth++;
      else if (ch === ")") depth--;
      cur += ch; i++;
    }
    parts.push(cur);
    return {parts: parts, joiner: joiner || "and"};
  }

  function _matchesAny(p) {
    return RE_IS.test(p) || RE_NOT_IS.test(p) || RE_CMP.test(p) || RE_BETWEEN.test(p) ||
           RE_TIME_AND.test(p) || RE_TIME_OR.test(p);
  }

  function templateToWhen(text) {
    if (typeof text !== "string") return null;
    var m = text.match(/^\s*\{\{\s*([\s\S]*?)\s*\}\}\s*$/);
    if (!m) return null;
    var inner = m[1];
    if (inner === "true") return {combine: "all", conditions: [], time: null};
    var split = _splitTop(inner);
    if (!split) return null;
    if (split.parts.length === 1 && _wraps(inner) && !_matchesAny(inner)) {
      // several conditions joined inside one pair of parens (what whenToTemplate
      // emits for >1 part) - a single parenthesized condition matches as-is above
      var inner2 = _splitTop(inner.slice(1, -1));
      if (inner2 && inner2.parts.length > 1) split = inner2;
    }
    var conditions = [], time = null;
    for (var i = 0; i < split.parts.length; i++) {
      var p = split.parts[i], mm;
      if ((mm = p.match(RE_NOT_IS))) {
        conditions.push({entity: _unq(mm[1]), op: "is not", value: _unq(mm[2])});
      } else if ((mm = p.match(RE_IS))) {
        conditions.push({entity: _unq(mm[1]), op: "is", value: _unq(mm[2])});
      } else if ((mm = p.match(RE_BETWEEN))) {
        if (_unq(mm[1]) !== _unq(mm[3])) return null;
        conditions.push({entity: _unq(mm[1]), op: "between",
                         value: Number(mm[2]), value2: Number(mm[4])});
      } else if ((mm = p.match(RE_CMP))) {
        if (mm[2] === ">") conditions.push({entity: _unq(mm[1]), op: "above", value: Number(mm[3])});
        else conditions.push({entity: _unq(mm[1]), op: "below", value: Number(mm[3])});
      } else if ((mm = p.match(RE_TIME_AND))) {
        if (time) return null;
        time = {from: mm[1], to: mm[2]};
      } else if ((mm = p.match(RE_TIME_OR))) {
        if (time) return null;
        time = {from: mm[1], to: mm[2]};
      } else {
        return null;
      }
    }
    for (var j = 0; j < conditions.length; j++) {
      if (!ENTITY_RE.test(conditions[j].entity)) return null;
    }
    return {combine: split.joiner === "or" ? "any" : "all", conditions: conditions, time: time};
  }

  var api = {whenToTemplate: whenToTemplate, templateToWhen: templateToWhen};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else global.WhenBuilder = api;
})(typeof window !== "undefined" ? window : globalThis);
