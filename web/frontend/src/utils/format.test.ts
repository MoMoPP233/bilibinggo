/* eslint-disable */
// @vitest-environment jsdom
// V14: format.ts 类型收紧前后的输出等价性快照（期望值由未加类型的 baseline 运行逐行捕获）。

import { describe, expect, it, vi } from "vitest";
import {
  lotteryTypeTone,
  activityStatusTone,
  formatUnixTimestamp,
  formatWatchWindow,
  formatWindowDays,
  formatToastDetail,
  formatLastParticipation,
  badgeClass,
  formatJobProgressDisplay,
  formatProgressTitle,
  formatProgressDetail,
  formatAccountStat,
  formatFilterSummary,
  formatHeat,
  formatLotteryTime,
} from "./format";
import { isLotterySoon, formatAutoCountdown } from "./format";

const EXP: Record<string, unknown[]> = {
  lotteryTypeTone: ["default", "default", "default", "interact", "repost", "reserve", "default", "interact"],
  activityStatusTone: ["joined", "ended", "pending", "pending", "pending", "pending"],
  formatUnixTimestamp: ["尚未同步", "尚未同步", "尚未同步", "尚未同步", "尚未同步", "尚未同步", "1970-01-01 07:43", "2025-08-12 20:00", "1970-01-01 08:00"],
  formatWatchWindow: ["08/12 20:00 — 08/12 21:00", "—", "—", "—", "—", "01/01 07:43 — 01/01 07:43"],
  formatWindowDays: ["1 天", "1 天", "1 天", "1 天", "1 天", "2 天", "1 天", "1 天", "2 天", "1 天"],
  formatToastDetail: ["", "", "", "", "a · b · long line here", "hello <b>bold</b> &amp; \"quote\"", ""],
  formatLastParticipation: ["", "", "", "参与成功", "搞定", "失败：点赞、评论（频控）", "失败：unknown_zzz", "参与失败", "已失败原因", "某消息", "other_state", "", "参与成功"],
  badgeClass: ["badge joined", "badge ended", "badge pending", "badge pending", "badge pending", "badge pending"],
  formatJobProgressDisplay: [{"percent": 30, "value": "30", "suffix": "%", "showPercentSuffix": true}, {"percent": 20, "value": "1/5", "suffix": " 步", "showPercentSuffix": false}, {"percent": 0, "value": "0", "suffix": "", "showPercentSuffix": false}, {"percent": 0, "value": "0", "suffix": "", "showPercentSuffix": false}, {"percent": 8, "value": "8", "suffix": "%", "showPercentSuffix": true}],
  formatProgressTitle: ["正在参与活动", "三连参与 · 并行 3 个活动", "三连参与 · 并行 3 个活动", "同步关注用户", "任务运行中…", "任务运行中…"],
  formatProgressDetail: ["准备开始参与…", "当前步骤：关注", "直接提示语", "并行处理中，请稍候…", "并行处理中，请稍候…", "请稍候，任务在后台执行中", "进行中…", "请稍候，任务在后台执行中"],
  formatAccountStat: [0, "—", "…", "—", 0, 0, "", 123, "42", "—"],
  formatFilterSummary: ["未找到匹配活动", "共 <strong class=\"filter-summary-count\">100</strong> 条 · 第 2/5 页", "未找到匹配活动"],
  formatHeat: ["0", "—", "1.5万", "9999", "0", "2.0万", "—", "0", "0"],
  formatLotteryTime: ["无开奖日期", "无开奖日期", "无开奖日期", "无开奖日期", "2026-09-06 13:30", "无开奖日期", "无开奖日期", "无开奖日期"],
};

describe("format equivalence (baseline snapshot)", () => {
  it("lotteryTypeTone", () => {
    expect(lotteryTypeTone("")).toEqual(EXP.lotteryTypeTone[0]);
    expect(lotteryTypeTone(null)).toEqual(EXP.lotteryTypeTone[1]);
    expect(lotteryTypeTone(null)).toEqual(EXP.lotteryTypeTone[2]);
    expect(lotteryTypeTone("互动抽奖")).toEqual(EXP.lotteryTypeTone[3]);
    expect(lotteryTypeTone("转发")).toEqual(EXP.lotteryTypeTone[4]);
    expect(lotteryTypeTone("预约直播")).toEqual(EXP.lotteryTypeTone[5]);
    expect(lotteryTypeTone("视频")).toEqual(EXP.lotteryTypeTone[6]);
    expect(lotteryTypeTone("xx互动x")).toEqual(EXP.lotteryTypeTone[7]);
  });
  it("activityStatusTone", () => {
    expect(activityStatusTone("已参加")).toEqual(EXP.activityStatusTone[0]);
    expect(activityStatusTone("已结束")).toEqual(EXP.activityStatusTone[1]);
    expect(activityStatusTone("未开奖")).toEqual(EXP.activityStatusTone[2]);
    expect(activityStatusTone("")).toEqual(EXP.activityStatusTone[3]);
    expect(activityStatusTone(null)).toEqual(EXP.activityStatusTone[4]);
    expect(activityStatusTone(null)).toEqual(EXP.activityStatusTone[5]);
  });
  it("formatUnixTimestamp", () => {
    expect(formatUnixTimestamp(0)).toEqual(EXP.formatUnixTimestamp[0]);
    expect(formatUnixTimestamp(null)).toEqual(EXP.formatUnixTimestamp[1]);
    expect(formatUnixTimestamp(null)).toEqual(EXP.formatUnixTimestamp[2]);
    expect(formatUnixTimestamp("")).toEqual(EXP.formatUnixTimestamp[3]);
    expect(formatUnixTimestamp("abc")).toEqual(EXP.formatUnixTimestamp[4]);
    expect(formatUnixTimestamp(Infinity)).toEqual(EXP.formatUnixTimestamp[5]);
    expect(formatUnixTimestamp(-1000)).toEqual(EXP.formatUnixTimestamp[6]);
    expect(formatUnixTimestamp(1755000000)).toEqual(EXP.formatUnixTimestamp[7]);
    expect(formatUnixTimestamp(1)).toEqual(EXP.formatUnixTimestamp[8]);
  });
  it("formatWatchWindow", () => {
    expect(formatWatchWindow(1755000000, 1755003600)).toEqual(EXP.formatWatchWindow[0]);
    expect(formatWatchWindow(0, 1755003600)).toEqual(EXP.formatWatchWindow[1]);
    expect(formatWatchWindow(null, null)).toEqual(EXP.formatWatchWindow[2]);
    expect(formatWatchWindow("", 1)).toEqual(EXP.formatWatchWindow[3]);
    expect(formatWatchWindow(null, "abc")).toEqual(EXP.formatWatchWindow[4]);
    expect(formatWatchWindow(-1000, -1000)).toEqual(EXP.formatWatchWindow[5]);
  });
  it("formatWindowDays", () => {
    expect(formatWindowDays(86400)).toEqual(EXP.formatWindowDays[0]);
    expect(formatWindowDays(0)).toEqual(EXP.formatWindowDays[1]);
    expect(formatWindowDays(null)).toEqual(EXP.formatWindowDays[2]);
    expect(formatWindowDays(null)).toEqual(EXP.formatWindowDays[3]);
    expect(formatWindowDays("")).toEqual(EXP.formatWindowDays[4]);
    expect(formatWindowDays(172800)).toEqual(EXP.formatWindowDays[5]);
    expect(formatWindowDays(90000)).toEqual(EXP.formatWindowDays[6]);
    expect(formatWindowDays(-86400)).toEqual(EXP.formatWindowDays[7]);
    expect(formatWindowDays("129600")).toEqual(EXP.formatWindowDays[8]);
    expect(formatWindowDays(NaN)).toEqual(EXP.formatWindowDays[9]);
  });
  it("formatToastDetail", () => {
    expect(formatToastDetail(null)).toEqual(EXP.formatToastDetail[0]);
    expect(formatToastDetail(null)).toEqual(EXP.formatToastDetail[1]);
    expect(formatToastDetail({})).toEqual(EXP.formatToastDetail[2]);
    expect(formatToastDetail({"log": ""})).toEqual(EXP.formatToastDetail[3]);
    expect(formatToastDetail({"log": "  a \n b \n  \nlong line here \n\n\n"})).toEqual(EXP.formatToastDetail[4]);
    expect(formatToastDetail({"log": "hello <b>bold</b> &amp; \"quote\""})).toEqual(EXP.formatToastDetail[5]);
    expect(formatToastDetail({"log": null})).toEqual(EXP.formatToastDetail[6]);
  });
  it("formatLastParticipation", () => {
    expect(formatLastParticipation(null)).toEqual(EXP.formatLastParticipation[0]);
    expect(formatLastParticipation(null)).toEqual(EXP.formatLastParticipation[1]);
    expect(formatLastParticipation({})).toEqual(EXP.formatLastParticipation[2]);
    expect(formatLastParticipation({"status": "joined", "message": ""})).toEqual(EXP.formatLastParticipation[3]);
    expect(formatLastParticipation({"status": "joined", "message": "搞定"})).toEqual(EXP.formatLastParticipation[4]);
    expect(formatLastParticipation({"status": "failed", "actions": [{"ok": false, "action": "like"}, {"ok": true, "action": "follow"}, {"ok": false, "action": "comment"}], "message": "频控"})).toEqual(EXP.formatLastParticipation[5]);
    expect(formatLastParticipation({"status": "failed", "actions": [{"ok": false, "action": "unknown_zzz"}], "message": ""})).toEqual(EXP.formatLastParticipation[6]);
    expect(formatLastParticipation({"status": "failed", "actions": [], "message": ""})).toEqual(EXP.formatLastParticipation[7]);
    expect(formatLastParticipation({"status": "failed", "actions": null, "message": "已失败原因"})).toEqual(EXP.formatLastParticipation[8]);
    expect(formatLastParticipation({"status": "other_state", "message": "某消息"})).toEqual(EXP.formatLastParticipation[9]);
    expect(formatLastParticipation({"status": "other_state", "message": ""})).toEqual(EXP.formatLastParticipation[10]);
    expect(formatLastParticipation({"status": "", "message": ""})).toEqual(EXP.formatLastParticipation[11]);
    expect(formatLastParticipation({"status": "joined"})).toEqual(EXP.formatLastParticipation[12]);
  });
  it("badgeClass", () => {
    expect(badgeClass("已参加")).toEqual(EXP.badgeClass[0]);
    expect(badgeClass("已结束")).toEqual(EXP.badgeClass[1]);
    expect(badgeClass("未开奖")).toEqual(EXP.badgeClass[2]);
    expect(badgeClass(null)).toEqual(EXP.badgeClass[3]);
    expect(badgeClass(null)).toEqual(EXP.badgeClass[4]);
    expect(badgeClass("")).toEqual(EXP.badgeClass[5]);
  });
  it("formatJobProgressDisplay", () => {
    expect(formatJobProgressDisplay({"action": "watch_users", "progress_step": 3, "progress_total": 10})).toEqual(EXP.formatJobProgressDisplay[0]);
    expect(formatJobProgressDisplay({"action": "participate", "progress_step": 1, "progress_total": 5})).toEqual(EXP.formatJobProgressDisplay[1]);
    expect(formatJobProgressDisplay({"action": "participate", "progress_step": 0, "progress_total": 0})).toEqual(EXP.formatJobProgressDisplay[2]);
    expect(formatJobProgressDisplay({"action": "participate_triple", "progress_step": 0, "progress_total": 0})).toEqual(EXP.formatJobProgressDisplay[3]);
    expect(formatJobProgressDisplay({})).toEqual(EXP.formatJobProgressDisplay[4]);
  });
  it("formatProgressTitle", () => {
    expect(formatProgressTitle({"action": "participate"})).toEqual(EXP.formatProgressTitle[0]);
    expect(formatProgressTitle({"action": "participate_triple", "progress_message": "", "result": {}})).toEqual(EXP.formatProgressTitle[1]);
    expect(formatProgressTitle({"action": "participate_triple", "progress_message": null, "result": null})).toEqual(EXP.formatProgressTitle[2]);
    expect(formatProgressTitle({"action": "watch_users", "label": "同步关注用户"})).toEqual(EXP.formatProgressTitle[3]);
    expect(formatProgressTitle({"action": "zzz", "label": ""})).toEqual(EXP.formatProgressTitle[4]);
    expect(formatProgressTitle({})).toEqual(EXP.formatProgressTitle[5]);
  });
  it("formatProgressDetail", () => {
    expect(formatProgressDetail({"action": "participate", "progress_message": "", "message": "", "progress_step": 0, "progress_total": 0})).toEqual(EXP.formatProgressDetail[0]);
    expect(formatProgressDetail({"action": "participate", "progress_message": "带管道|的message", "message": "", "progress_step": 2, "progress_total": 0})).toEqual(EXP.formatProgressDetail[1]);
    expect(formatProgressDetail({"action": "participate", "progress_message": null, "message": "直接提示语", "progress_step": 0, "progress_total": 0})).toEqual(EXP.formatProgressDetail[2]);
    expect(formatProgressDetail({"action": "participate_triple", "progress_message": "", "progress_step": 0})).toEqual(EXP.formatProgressDetail[3]);
    expect(formatProgressDetail({"action": "participate_triple", "progress_message": null, "progress_step": 0})).toEqual(EXP.formatProgressDetail[4]);
    expect(formatProgressDetail({"action": "watch_users", "progress_message": null, "message": null})).toEqual(EXP.formatProgressDetail[5]);
    expect(formatProgressDetail({"action": "watch_users", "progress_message": "进行中…", "message": "备用"})).toEqual(EXP.formatProgressDetail[6]);
    expect(formatProgressDetail({})).toEqual(EXP.formatProgressDetail[7]);
  });
  it("formatAccountStat", () => {
    expect(formatAccountStat(null, true, false)).toEqual(EXP.formatAccountStat[0]);
    expect(formatAccountStat(null, false, false)).toEqual(EXP.formatAccountStat[1]);
    expect(formatAccountStat(null, true, true)).toEqual(EXP.formatAccountStat[2]);
    expect(formatAccountStat(null, false, true)).toEqual(EXP.formatAccountStat[3]);
    expect(formatAccountStat(0, true, false)).toEqual(EXP.formatAccountStat[4]);
    expect(formatAccountStat(0, false, false)).toEqual(EXP.formatAccountStat[5]);
    expect(formatAccountStat("", true, false)).toEqual(EXP.formatAccountStat[6]);
    expect(formatAccountStat(123, true, false)).toEqual(EXP.formatAccountStat[7]);
    expect(formatAccountStat("42", true, false)).toEqual(EXP.formatAccountStat[8]);
    expect(formatAccountStat(null, false, true)).toEqual(EXP.formatAccountStat[9]);
  });
  it("formatFilterSummary", () => {
    expect(formatFilterSummary({"total": 0, "page": 1, "pages": 1})).toEqual(EXP.formatFilterSummary[0]);
    expect(formatFilterSummary({"total": 100, "page": 2, "pages": 5})).toEqual(EXP.formatFilterSummary[1]);
    expect(formatFilterSummary({"total": "", "page": null, "pages": null})).toEqual(EXP.formatFilterSummary[2]);
  });
  it("formatHeat", () => {
    expect(formatHeat({})).toEqual(EXP.formatHeat[0]);
    expect(formatHeat({"heat_missing": 1})).toEqual(EXP.formatHeat[1]);
    expect(formatHeat({"repost_count": 15000})).toEqual(EXP.formatHeat[2]);
    expect(formatHeat({"repost_count": 9999})).toEqual(EXP.formatHeat[3]);
    expect(formatHeat({"repost_count": 0})).toEqual(EXP.formatHeat[4]);
    expect(formatHeat({"repost_count": "20000"})).toEqual(EXP.formatHeat[5]);
    expect(formatHeat({"heat_missing": true, "repost_count": 100})).toEqual(EXP.formatHeat[6]);
    expect(formatHeat({"repost_count": NaN})).toEqual(EXP.formatHeat[7]);
    expect(formatHeat({"repost_count": null})).toEqual(EXP.formatHeat[8]);
  });
  it("formatLotteryTime", () => {
    expect(formatLotteryTime("")).toEqual(EXP.formatLotteryTime[0]);
    expect(formatLotteryTime(null)).toEqual(EXP.formatLotteryTime[1]);
    expect(formatLotteryTime(null)).toEqual(EXP.formatLotteryTime[2]);
    expect(formatLotteryTime("—")).toEqual(EXP.formatLotteryTime[3]);
    expect(formatLotteryTime("2026-09-06 13:30")).toEqual(EXP.formatLotteryTime[4]);
    expect(formatLotteryTime("2026/09/06 13:30")).toEqual(EXP.formatLotteryTime[5]);
    expect(formatLotteryTime("2026-09-06 9:30")).toEqual(EXP.formatLotteryTime[6]);
    expect(formatLotteryTime("xxxx")).toEqual(EXP.formatLotteryTime[7]);
  });
});

describe("format clock-sensitive paths (deterministic fake clock)", () => {
  it("isLotterySoon window boundaries", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 8, 6, 0, 0, 0));
    const fmt = (d: Date) => {
      const pad = (n: number) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    const now = new Date(Date.now());
    const twoDays = new Date(now.getTime() + 2 * 86400000);
    const fourDays = new Date(now.getTime() + 4 * 86400000);
    expect(isLotterySoon(fmt(twoDays))).toBe(true);
    expect(isLotterySoon(fmt(fourDays))).toBe(false);
    expect(isLotterySoon(fmt(now))).toBe(true);
    expect(isLotterySoon(fmt(new Date(now.getTime() - 60000)))).toBe(false);
    expect(isLotterySoon("2000-01-01 00:00")).toBe(false);
    expect(isLotterySoon("")).toBe(false);
    expect(isLotterySoon(null)).toBe(false);
    expect(isLotterySoon("2026-13-40 99:99")).toBe(false);
    expect(isLotterySoon("2026-09-06")).toBe(false);
    expect(isLotterySoon("not a time")).toBe(false);
    vi.useRealTimers();
  });

  it("formatAutoCountdown offsets vs fixed clock", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 8, 6, 0, 0, 0));
    expect(formatAutoCountdown(0)).toBe("—");
    expect(formatAutoCountdown("")).toBe("—");
    expect(formatAutoCountdown(null)).toBe("—");
    expect(formatAutoCountdown(undefined)).toBe("—");
    const base = Math.floor(Date.now() / 1000);
    expect(formatAutoCountdown(base)).toBe("00:00");
    expect(formatAutoCountdown(base + 45)).toBe("00:45");
    expect(formatAutoCountdown(base + 3661)).toBe("1:01:01");
    expect(formatAutoCountdown(base + 86399)).toBe("23:59:59");
    expect(formatAutoCountdown(base + 86460)).toBe("24:01:00");
    expect(formatAutoCountdown(base - 1000)).toBe("00:00");
    expect(formatAutoCountdown("abc")).toBe("NaN:NaN");
    vi.useRealTimers();
  });
});

