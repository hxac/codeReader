# linkPlugin：链接渲染、Wiki 链接与 URL 安全

## 1. 本讲目标

本讲讲解链接子系统 `linkPlugin`。学完后你应该能够：

- 说清**标准链接** `[text](url)` 与 **Wiki 链接** `[[target|display]]` 走的是两条完全不同的解析路径，并解释为什么必须这样。
- 看懂 `SKIP_PARENT_TYPES` 如何用「先收集区间、再逐个判定」的方式把代码块内部的链接排除掉。
- 读懂 `isSafeUrl` / `sanitizeUrl` 如何拦截 `javascript:`、`vbscript:`、`data:text/html` 等危险协议，以及 `encodeURI` 与 `try/catch` 兜底的安全意义。
- 把第 5 单元（u5）学到的 Widget 渲染、第 2 单元（u2）学到的 `shouldShowSource` 与拖拽冻结，融会贯通到一个真实插件里。

## 2. 前置知识

本讲是「图片与链接」单元（u6）的最后一篇，承接前面已建立的认知，不再重复细节，只做衔接：

- **`shouldShowSource`（u2-l2）**：决定一段区间显示源码还是渲染结果的纯函数。光标/选区与区间相交返回 `true`（显源码），拖拽中一律返回 `false`。本讲的链接在「渲染态 / 源码态」之间切换，完全由它裁决。
- **`mouseSelectingField`（u2-l3）**：记录「是否正在拖选」的 StateField。链接插件在拖拽期间冻结装饰，防止渲染态 widget 随选区抖动。
- **`WidgetType` 与 `Decoration.replace`（u3-l1）**：渲染态用 `Decoration.replace` 把链接原文替换成 `LinkWidget.toDOM` 生成的 `<a>` 元素；源码态用 `Decoration.mark` 套类名。`eq` 是重建闸门。
- **`ViewPlugin`（u2-l1）**：链接用不带 `block` 的行内 `replace` 装饰，所以走 `ViewPlugin` 而非 `StateField`。

一句话回顾：链接渲染 = 「找到链接区间 → 用 `shouldShowSource` 二选一 → 渲染态替换成可点击 `<a>`，源码态加底色标记」，外加一层 URL 安全净化。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/plugins/link.ts` | 链接插件的全部编排逻辑：两个解析函数、跳过区间收集、装饰构建、`linkPlugin` 工厂与 `update` 调度。 |
| `src/widgets/linkWidget.ts` | `LinkWidget`（`WidgetType` 子类）负责生成 `<a>` DOM；`LinkData` / `LinkOptions` 类型；**安全核心** `isSafeUrl` / `sanitizeUrl` 也在这个文件里。 |
| `src/plugins/__tests__/link.test.ts` | 链接插件的测试，覆盖两个解析函数的各种边界（空文本、单引号标题、URL 内含括号等），是理解正则行为的最佳捷径。 |
| `src/core/shouldShowSource.ts` | 渲染 / 源码态的决策函数（前置讲义已详解，本讲引用其判定逻辑）。 |
| `src/core/mouseSelecting.ts` | 拖拽状态字段（前置讲义已详解，本讲引用）。 |
| `src/theme/default.ts` | 链接相关样式类（`cm-link-widget`、`cm-wikilink-widget`、`cm-link-source` 等）的真实外观定义。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：① 两种链接的两条解析路径；② `skipRanges` 跳过代码块；③ URL 净化与危险协议拦截。

### 4.1 标准链接 vs Wiki 链接：两条解析路径

#### 4.1.1 概念说明

Markdown 里常见的链接写法是「标准链接」：

```markdown
[CodeMirror 官网](https://codemirror.net)
[GitHub](https://github.com "访问 GitHub")
```

而 Obsidian / Wiki 风格还有一种「Wiki 链接」：

```markdown
[[内部页面]]
[[目标页面|显示文字]]
```

这两种语法长得不一样，更关键的是——**Lezer 的 Markdown 语法树只认识标准链接，根本不认识 Wiki 链接**。如果你打开 DevTools 看 `[[Page]]` 在语法树里是什么节点，会发现它只是普通文本，没有任何「Wiki 节点」包裹它。

这就逼出了两条完全不同的解析路径：

| 维度 | 标准链接 | Wiki 链接 |
| --- | --- | --- |
| 识别方式 | 遍历语法树，找 `Link` 节点 | 对整篇文档做**正则全文扫描** |
| 信任程度 | 信任 Lezer 已经切好的区间边界 | 自己用正则切区间 |
| 为什么 | Lezer 原生支持，省事且准确 | Lezer 不支持，只能手动补 |
| 原文获取 | `state.doc.sliceString(from, to)` | `match[0]`（正则匹配到的子串） |

#### 4.1.2 核心流程

`buildLinkDecorations` 里两条路径的执行顺序：

```
1. 收集 skipRanges（见 4.2）
2. 遍历语法树：
     命中 node.name === 'Link'
       → 在 skip 区间内？跳过
       → parseLinkSyntax(原文)
       → shouldShowSource 决定渲染态/源码态
3. 用 WIKI_LINK_REGEX 对整篇文档全文扫描：
     每命中一处
       → 在 skip 区间内？跳过
       → parseWikiLink(match[0])
       → shouldShowSource 决定渲染态/源码态
4. 合并、排序、返回 DecorationSet
```

注意第 2 步依赖语法树节点名，第 3 步完全不依赖语法树——这是两者最本质的区别。

#### 4.1.3 源码精读

先看标准链接解析函数 [src/plugins/link.ts:30-51](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L30-L51)：

```ts
export function parseLinkSyntax(text: string): LinkData | null {
  if (text.startsWith('!')) return null;          // 排除图片语法 ![](url)
  const match = text.match(
    /^\[([^\]]*)\]\((.+?)(?:\s+["']([^"']+)["'])?\)$/
  );
  if (!match) return null;
  const [, linkText, url, title] = match;
  return { text: linkText, url, title, isWikiLink: false };
}
```

正则拆解（这是本模块最需要吃透的地方）：

- `\[([^\]]*)\]` —— 链接文字，`[^\]]*` 允许空文本（`[](...)` 合法）。
- `\((.+?)` —— URL，非贪婪，至少 1 个字符。
- `(?:\s+["']([^"']+)["'])?` —— **可选**的标题，前面必须有空白，标题用单引号或双引号包裹。
- `\)$` —— 收尾的右括号必须落在字符串末尾。

两个细节值得记：① `text.startsWith('!')` 主动排除图片，因为图片 `![alt](url)` 在语法树里也含 `Link` 子结构，但应交给 `imageField`（u6-l1）处理；② URL 用非贪婪 `(.+?)` 配合结尾的 `\)$`，这正是测试用例「URL 内含括号」`https://example.com/page_(1)` 能正确切出 URL 的原因——非贪婪让正则把**最后一个** `)` 留给收尾（见测试 [src/plugins/__tests__/link.test.ts:213-217](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/link.test.ts#L213-L217)）。

再看 Wiki 链接解析函数 [src/plugins/link.ts:59-74](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L59-L74)：

```ts
export function parseWikiLink(text: string): LinkData | null {
  const match = text.match(/^\[\[([^\]|]+)(?:\|([^\]]+))?\]\]$/);
  if (!match) return null;
  const [, target, display] = match;
  return {
    text: display || target,   // 有显示文字用它，否则用 target
    url: target,               // url 永远是 target
    isWikiLink: true,
  };
}
```

正则 `^\[\[([^\]|]+)(?:\|([^\]]+))?\]\]$`：target 不允许含 `]` 或 `|`，可选的 `|display` 不允许含 `]`。注意 `text: display || target`——没有显示文字时，链接文字回退成 target 本身，所以 `[[Page]]` 显示成「Page」。

因为 Lezer 不识别这种语法，插件用一条**带 `g` 标志的全局正则**对整篇文档扫描（[src/plugins/link.ts:79](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L79)）：

```ts
const WIKI_LINK_REGEX = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;
```

扫描循环在 [src/plugins/link.ts:163-196](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L163-L196)。一个容易踩的坑：带 `g` 标志的正则有**可变的 `lastIndex`**，跨次调用会「续读」导致漏匹配。源码在循环前显式 `WIKI_LINK_REGEX.lastIndex = 0`（[src/plugins/link.ts:163](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L163)）重置游标，正是为了规避这个陷阱。

> 提示：`parseLinkSyntax` 和 `parseWikiLink` 都是**纯函数**（输入字符串、输出 `LinkData | null`，无副作用），所以可以脱离 CodeMirror 单独测试。测试文件里它们各自有独立 `describe` 块（[src/plugins/__tests__/link.test.ts:43-129](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/link.test.ts#L43-L129)），是验证你对正则理解的最佳参照。

#### 4.1.4 代码实践

**实践目标**：用真实测试验证你对两个解析函数正则边界的理解。

**操作步骤**：

1. 打开 `src/plugins/__tests__/link.test.ts`，通读 `parseLinkSyntax` 和 `parseWikiLink` 两个 `describe` 块的全部用例。
2. 在 `parseLinkSyntax` 的 `describe` 块末尾追加一个边界用例（示例代码，非项目原有代码）：

   ```ts
   it('should capture URL with trailing parentheses', () => {
     const result = parseLinkSyntax('[text](https://example.com/page_(1))');
     expect(result?.url).toBe('https://example.com/page_(1)');
   });
   ```

3. 在 `parseWikiLink` 的 `describe` 块末尾追加一个显示文字用例：

   ```ts
   it('should fall back to target when no display text', () => {
     const result = parseWikiLink('[[My Page]]');
     expect(result?.text).toBe('My Page');
     expect(result?.url).toBe('My Page');
   });
   ```

4. 运行 `npm test -- link`。

**需要观察的现象**：两个新用例应当通过。如果你把预期改成 `result?.url` 为 `https://example.com/page_(1`（少一个括号），用例会失败——这说明非贪婪正则确实把最后一个 `)` 留给了收尾的 `\)$`。

**预期结果**：两条测试通过，证明你对「非贪婪 + 结尾锚定决定 URL 切分」和「display 缺省回退 target」的理解正确。

#### 4.1.5 小练习与答案

**练习 1**：`parseLinkSyntax('[text](url1) (url2)')` 会返回什么？为什么？

**参考答案**：返回 `null`。因为正则要求 `\)$` 把右括号钉在字符串末尾，而这里的字符串在 `)` 之后还有 ` (url2)`，整体无法匹配。这也提醒：`parseLinkSyntax` 只接受「整段文本恰好是一个完整链接」的输入，片段不收。

**练习 2**：`parseWikiLink('[[a|b|c]]')` 的 `text` 和 `url` 分别是什么？

**参考答案**：`url = 'a'`，`text = 'b|c'`。因为 target 部分 `[^\]|]+` 在遇到第一个 `|` 时停止，之后 `(?:\|([^\]]+))?` 把 `b|c`（display 允许含 `|`，只禁止 `]`）整体捕获。

---

### 4.2 skipRanges：跳过代码块的范围收集

#### 4.2.1 概念说明

代码块（` ``` ` 围栏块、缩进代码块）和行内代码里，经常会写一些「看起来像链接」的文本，例如：

````markdown
```text
示例：[click](javascript:alert(1))
```
````

这些显然不该被渲染成可点击链接——它们是代码，应当原样显示。但问题是：

- 标准链接 `[click](...)` 在代码块里**仍可能被 Lezer 解析成 `Link` 节点**（取决于代码块是否被识别为代码内容）。
- Wiki 链接走的是**正则全文扫描**，正则不区分代码块内外，会把代码块里的 `[[Page]]` 也抓出来。

因此需要一个统一的「跳过区间」机制：先把所有代码类节点的范围收集起来，再在两条解析路径上都判定「这个链接是否落在某个代码区间内」。

#### 4.2.2 核心流程

```
第一遍遍历语法树，收集 skipRanges：
  每遇到 FencedCode / CodeBlock / InlineCode 节点
    → 把 {from, to} 推入 skipRanges

定义 isInSkipRange(from, to)：
  若存在某个 skip 区间 r，满足 from >= r.from 且 to <= r.to
    → 返回 true（被完全包含）

标准链接路径、Wiki 链接路径，在产出装饰前都先调用 isInSkipRange，
  命中则跳过该链接。
```

判定用的是「完全包含」而非「相交」：只有当链接整个落在代码块内部才跳过。这样跨界的极端情况不会被误伤。

#### 4.2.3 源码精读

跳过名单是一个 `Set`（[src/plugins/link.ts:93](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L93)）：

```ts
const SKIP_PARENT_TYPES = new Set(['FencedCode', 'CodeBlock', 'InlineCode']);
```

三种类型分别对应：围栏代码块（` ``` `）、缩进代码块（行首 4 空格）、行内代码（`` `code` ``）。注意这里**不包含** `math` 相关节点——数学公式里的链接由 `mathPlugin` 自行管辖（见 u3-l2、u3-l3），各插件靠「节点类型名单」划分管辖权，这是本库反复出现的模块化手法。

收集与判定逻辑在 [src/plugins/link.ts:107-119](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L107-L119)：

```ts
const skipRanges: Array<{ from: number; to: number }> = [];
syntaxTree(state).iterate({
  enter: (node) => {
    if (SKIP_PARENT_TYPES.has(node.name)) {
      skipRanges.push({ from: node.from, to: node.to });
    },
  },
});

const isInSkipRange = (from: number, to: number) =>
  skipRanges.some((r) => from >= r.from && to <= r.to);
```

注意这套机制对两条路径**一视同仁**：标准链接路径在 [src/plugins/link.ts:126-128](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L126-L128) 判定，Wiki 链接路径在 [src/plugins/link.ts:170-172](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/link.ts#L170-L172) 判定。正则扫描本不管代码块，是 `isInSkipRange` 给它补上了这层保护。

> 延伸：这个「先收区间、再逐个判定」的模式和 u6-l1 图片插件无关，但和表格/代码块里「区间数组管理」的思路一脉相承——都是用一组 `{from,to}` 区间做范围过滤。

#### 4.2.4 代码实践

**实践目标**：在真实文档里观察「代码块内外的链接分别被如何处理」。

**操作步骤**：

1. 进入 `demo` 目录运行 `npm run dev`，在浏览器打开页面（参考 u1-l3）。
2. 找到启用 `linkPlugin` 的那个编辑器（如「full」配置）。
3. 输入以下内容（注意第二行在代码块内）：

   ````markdown
   普通链接 [docs](https://codemirror.net) 在这里

   ```text
   代码块内 [fake](https://evil.example) 和 [[NotWiki]]
   ```
   ````

4. 把光标移到这三段的不同位置，观察渲染效果。

**需要观察的现象**：

- 第 1 行的 `[docs](...)` 在光标离开时变成可点击的蓝色文字（渲染态），光标进入时显示带底色的源码。
- 代码块内的 `[fake](...)` 和 `[[NotWiki]]` **始终是纯文本**，不会被替换成链接，也不会变色。

**预期结果**：代码块内的链接被 `isInSkipRange` 拦截，两条路径都不产出装饰；代码块外的链接正常渲染。若观察不到，说明该编辑器未启用 `linkPlugin`（demo 的 basic 配置不含它）。

> 「待本地验证」：上述现象需在本地 demo 中实际确认。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `SKIP_PARENT_TYPES` 里删掉 `'InlineCode'`，行内代码 `` `[text](url)` `` 会发生什么？

**参考答案**：行内代码不再被收集进 skipRanges，其中的 `[text](url)` 若被 Lezer 解析为 `Link` 节点，就会被 `linkPlugin` 渲染成可点击链接——这违反了「代码应原样显示」的预期。这也说明跳过名单是与渲染正确性强相关的。

**练习 2**：`isInSkipRange` 为什么用「完全包含」（`from >= r.from && to <= r.to`）而不是「相交」？

**参考答案**：完全包含更保守。如果一个链接恰好跨越代码块边界（极端边界情况），相交判定会把它跳过，可能误伤合法链接；完全包含只在链接整个位于代码块内时跳过，减少误杀。代码块边界与链接边界精确重叠在 Markdown 里几乎不会自然发生。

---

### 4.3 URL 净化与危险协议拦截

#### 4.3.1 概念说明

把用户写的 URL 直接塞进 `<a href>` 是危险的。考虑这段 Markdown：

```markdown
[点我](javascript:alert(document.cookie))
[看图](data:text/html,<script>alert(1)</script>)
```

如果原样渲染成 `<a href="javascript:alert(...)">点我</a>`，用户一点击就会执行注入的脚本——这是典型的 **XSS（跨站脚本）**。链接插件必须把这类「可执行协议」挡在门外。

`linkWidget.ts` 用两道防线：

1. **`isSafeUrl`**：维护一个危险协议黑名单，URL 以这些开头即为不安全。
2. **`sanitizeUrl`**：不安全就丢弃（返回空串）；安全的再过一遍 `encodeURI` 做字符编码，并用 `try/catch` 兜住畸形 URL 的异常。

此外还有一道隐性防线：链接**文字**始终用 `textContent` 而非 `innerHTML` 写入 DOM，从根本上杜绝通过文字注入 HTML。

> 概念区分：黑名单（blocklist）只拦截已知危险协议，允许其他一切通过；白名单（allowlist）则只放行已知安全协议。本项目对链接采用**黑名单**策略，因为它要兼容 `http(s)`、相对路径、`mailto:`、`tel:` 等各种合法形态——白名单会过度限制。

#### 4.3.2 核心流程

标准链接渲染时（在 `LinkWidget.toDOM` 内）：

```
取 url
  → sanitizeUrl(url)
       → isSafeUrl(url)?
            否 → 返回 ''（href 不设置，<a> 不可导航）
            是 → encodeURI(url)（异常则返回 ''）
  → 若结果非空，赋给 anchor.href
  → 若 openInNewTab，设 target=_blank + rel=noopener noreferrer
```

Wiki 链接走的是**另一条路**：`href` 直接设为空串 `''`，导航完全交给应用层提供的 `onWikiLinkClick(url)` 回调，点击时 `preventDefault()`。所以 Wiki 链接的 `url`（即 target）**不经过** `sanitizeUrl`——它不是真的 URL，而是应用自定义解析的页面标识。

#### 4.3.3 源码精读

危险协议黑名单与安全判定在 [src/widgets/linkWidget.ts:38-46](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/linkWidget.ts#L38-L46)：

```ts
const DANGEROUS_PROTOCOLS = ['javascript:', 'vbscript:', 'data:text/html'];

function isSafeUrl(url: string): boolean {
  const lowerUrl = url.toLowerCase().trim();
  return !DANGEROUS_PROTOCOLS.some((protocol) => lowerUrl.startsWith(protocol));
}
```

三个要点：

- **大小写无关**：先 `toLowerCase()`，所以 `JavaScript:`、`JAVASCRIPT:` 都能拦住。
- **去首尾空白**：`trim()` 防止 `  javascript:` 这种前导空格绕过。
- **精准拦截 `data:text/html` 而非全部 `data:`**：因为图片（u6-l1）需要用 `data:image/png;base64,...` 这类合法 data URL，一刀切禁 `data:` 会误伤图片；而 `data:text/html` 能携带可执行 HTML，必须拦。这是协议级的精细取舍。

净化函数 [src/widgets/linkWidget.ts:51-61](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/linkWidget.ts#L51-L61)：

```ts
function sanitizeUrl(url: string): string {
  if (!isSafeUrl(url)) return '';      // 危险协议 → 丢弃
  try {
    return encodeURI(url);             // 编码特殊字符
  } catch {
    return '';                         // 畸形 URL（如坏代理对）→ 丢弃
  }
}
```

`encodeURI` 会把空格、中文等「不安全字符」百分号编码，使 URL 规范化；它对包含未配对代理对的字符串会抛 `URIError`，`try/catch` 把这种情况也降级为空串——**任何异常都不让链接变成可点击的坏 URL**。

净化结果在 `toDOM` 的标准链接分支里使用（[src/widgets/linkWidget.ts:111-124](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/linkWidget.ts#L111-L124)）：

```ts
const safeUrl = sanitizeUrl(url);
if (safeUrl) anchor.href = safeUrl;            // 不安全则不设 href
if (openInNewTab) {
  anchor.target = '_blank';
  anchor.rel = 'noopener noreferrer';          // 防止新标签页反向引用原页面
}
```

注意 `if (safeUrl)`：危险 URL 被净化成空串后，`anchor` 没有 `href`，浏览器不会把它当作可导航链接——文字仍显示（`textContent`），但点不动。`rel="noopener noreferrer"` 是新开标签页场景下的安全标配，防止新页面通过 `window.opener` 操纵原页面。

Wiki 链接分支（[src/widgets/linkWidget.ts:100-110](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/linkWidget.ts#L100-L110)）刻意不设真实 `href`：

```ts
if (isWikiLink) {
  anchor.className = 'cm-link-widget cm-wikilink-widget';
  anchor.href = '';
  anchor.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    onWikiLinkClick?.(url);   // 交给应用层解析 target
  });
}
```

最后看 `eq`（[src/widgets/linkWidget.ts:77-83](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/linkWidget.ts#L77-L83)）：

```ts
eq(other: LinkWidget): boolean {
  return (
    other.data.text === this.data.text &&
    other.data.url === this.data.url &&
    other.data.isWikiLink === this.data.isWikiLink
  );
}
```

回忆 u3-l1 的心法：「`eq` 比较的字段集合须等于决定 `toDOM` 输出的字段集合」。这里比较了 `text/url/isWikiLink`，但 **`toDOM` 还用到 `title`**（`anchor.title = title || ''`）和若干 `options`。也就是说，仅 `title` 变化时 `eq` 会误判为「相等」、复用旧 DOM，导致旧 title 残留——这是与心法的一处微小偏差，读源码时值得留意（详见 4.3.5 练习 2）。

#### 4.3.4 代码实践

**实践目标**：亲手验证危险协议被拦截、普通链接被放行。

**操作步骤**：

1. 在 `src/plugins/__tests__/link.test.ts` 的 `parseLinkSyntax` 用例区追加（示例代码）：

   ```ts
   it('should parse dangerous protocol URL as a normal link text', () => {
     const result = parseLinkSyntax('[click](javascript:alert(1))');
     expect(result?.text).toBe('click');
     expect(result?.url).toBe('javascript:alert(1)'); // 解析层不拦截，照原样返回
   });
   ```

2. 在测试文件顶部 `vi.mock` 之外，新增一个针对 `LinkWidget` 真实净化的用例。由于 `isSafeUrl`/`sanitizeUrl` 未导出，可间接通过 `createLinkWidget(...).toDOM()` 观察 `href`（需把现有 `vi.mock('../../widgets/linkWidget')` 临时去掉，或新建一个不带 mock 的测试文件验证）。简化做法——直接断言导出的 widget 行为（示例代码）：

   ```ts
   import { createLinkWidget } from '../../widgets/linkWidget';

   it('should drop href for dangerous protocol', () => {
     const a = createLinkWidget(
       { text: 'x', url: 'javascript:alert(1)', isWikiLink: false },
       { openInNewTab: false }
     ).toDOM() as HTMLAnchorElement;
     expect(a.getAttribute('href')).toBeNull();
   });

   it('should keep href for safe http link', () => {
     const a = createLinkWidget(
       { text: 'x', url: 'https://example.com', isWikiLink: false },
       { openInNewTab: false }
     ).toDOM() as HTMLAnchorElement;
     expect(a.getAttribute('href')).toBe('https://example.com');
   });
   ```

3. 运行 `npm test -- link`。

**需要观察的现象**：

- `javascript:` 的 URL 在 **解析层**（`parseLinkSyntax`）原样保留（解析只认语法、不管安全），真正拦截发生在 **渲染层**（`toDOM` → `sanitizeUrl`）——这种「解析与安全分离」的分层是本模块的核心设计。
- 危险链接的 `<a>` 没有 `href` 属性；安全链接的 `href` 被正确设置。

**预期结果**：三条用例全部通过，验证了「解析不拦截、渲染才拦截」的分层。

> 「待本地验证」：第 2 步若沿用文件顶部的 `vi.mock`，需调整为不 mock `linkWidget`，或把这两个用例放进独立文件，否则 `createLinkWidget` 是 mock 版、不会真正净化。

#### 4.3.5 小练习与答案

**练习 1**：`isSafeUrl('  JaVaScRiPt:alert(1)')` 返回什么？为什么？

**参考答案**：返回 `false`。因为 `isSafeUrl` 先 `toLowerCase().trim()` 得到 `javascript:alert(1)`，它以 `javascript:` 开头，命中黑名单。大小写与首尾空白都无法绕过。

**练习 2**：根据 u3-l1 的 `eq` 心法，`LinkWidget.eq` 漏比 `title` 会带来什么后果？

**参考答案**：若用户把 `[a](url "t1")` 改成 `[a](url "t2")`（仅标题变化），`eq` 因 `text/url/isWikiLink` 都没变而返回 `true`，CodeMirror 复用旧 DOM，于是 `anchor.title` 仍是旧值 `t1`，直到下一次必须重建时才更新。这是一个轻微的「标题陈旧」问题；严格遵循心法应把 `title` 也纳入 `eq` 比较。

**练习 3**：为什么 Wiki 链接的 `url` 不经过 `sanitizeUrl`，反而是安全的？

**参考答案**：Wiki 链接的 `href` 直接设为空串，不参与浏览器导航；点击被 `preventDefault()` 取消，转交给应用层 `onWikiLinkClick(url)` 回调。target 字符串只是「页面标识」，如何解析成真实 URL 完全由宿主应用决定，没有进入 DOM 的 `href`，也就没有了 `javascript:` 之类的执行通道。

---

## 5. 综合实践

把三个最小模块串起来，完成规格指定的综合任务：**构造三个链接测试用例，分别走完「解析 → 跳过判定 → 净化」全链路**。

**实践目标**：用一张表格说清三种链接在 `parseLinkSyntax` / `parseWikiLink`、`isInSkipRange`、`sanitizeUrl` 三个环节各自的命运。

**操作步骤**：

1. 新建一个临时测试文件 `src/plugins/__tests__/linkTrace.test.ts`（示例代码，验证完可删除，不要提交到讲义外）。
2. 写入下面的对照断言（先自己预测「预期结果」列，再填进 `expect`）：

   ```ts
   import { describe, it, expect } from 'vitest';
   import { parseLinkSyntax, parseWikiLink } from '../link';
   import { createLinkWidget } from '../../widgets/linkWidget';

   describe('三种链接全链路', () => {
     it('危险协议：解析放行、渲染拦截', () => {
       const data = parseLinkSyntax('[click](javascript:alert(1))')!;
       const a = createLinkWidget(data, { openInNewTab: true }).toDOM() as HTMLAnchorElement;
       expect(data.url).toBe('javascript:alert(1)');
       expect(a.getAttribute('href')).toBeNull();   // 渲染层拦截
     });

     it('普通 http：全程放行', () => {
       const data = parseLinkSyntax('[docs](https://codemirror.net)')!;
       const a = createLinkWidget(data, { openInNewTab: true }).toDOM() as HTMLAnchorElement;
       expect(a.getAttribute('href')).toBe('https://codemirror.net');
       expect(a.target).toBe('_blank');
     });

     it('Wiki 链接：走正则解析、href 为空、靠回调', () => {
       const data = parseWikiLink('[[Internal Page]]')!;
       let clicked = '';
       const a = createLinkWidget(data, {
         onWikiLinkClick: (u) => { clicked = u; },
       }).toDOM() as HTMLAnchorElement;
       expect(data.isWikiLink).toBe(true);
       expect(a.getAttribute('href')).toBe('');     // 不导航
       a.click();                                    // 模拟点击
       expect(clicked).toBe('Internal Page');        // 回调收到 target
     });
   });
   ```

3. 运行 `npm test -- linkTrace`。

**预期结果**——填表（先遮住答案列自己写）：

| 输入 | 解析路径 | 解析结果 | `sanitizeUrl` | `<a href>` |
| --- | --- | --- | --- | --- |
| `[click](javascript:alert(1))` | `parseLinkSyntax` | `{text:'click', url:'javascript:alert(1)', isWikiLink:false}` | 返回 `''`（危险） | 不设置（`null`） |
| `[docs](https://codemirror.net)` | `parseLinkSyntax` | `{text:'docs', url:'https://codemirror.net', isWikiLink:false}` | 返回 `https://codemirror.net` | `https://codemirror.net` |
| `[[Internal Page]]` | `parseWikiLink` | `{text:'Internal Page', url:'Internal Page', isWikiLink:true}` | 不经过（Wiki 不用它） | `''`（空串） |

**核心结论**：解析层（`parseLinkSyntax`/`parseWikiLink`）只管「认语法」，对所有 URL 一视同仁、不拦截；安全拦截发生在渲染层（`sanitizeUrl`），且 Wiki 链接因走回调通道而天然规避了 URL 注入。这条「解析与安全分层」的界线，是本实践最重要的收获。

> 「待本地验证」：上述断言需在本地 jsdom 环境实跑确认；`a.click()` 触发的是 widget 内注册的 `click` 监听，需确保未被外层 mock 影响。

## 6. 本讲小结

- `linkPlugin` 是 `ViewPlugin`，用不带 `block` 的行内 `Decoration.replace` 渲染链接，故走 ViewPlugin 而非 StateField。
- **两条解析路径**：标准链接 `[text](url)` 信任 Lezer 语法树的 `Link` 节点；Wiki 链接 `[[target|display]]` 因 Lezer 不支持，用带 `g` 标志的全局正则对整篇文档扫描（扫描前须重置 `lastIndex`）。
- **跳过代码块**：用 `SKIP_PARENT_TYPES`（`FencedCode`/`CodeBlock`/`InlineCode`）先收集 `skipRanges`，再用「完全包含」判定 `isInSkipRange`，两条解析路径共用这套保护。
- **显示哪一面**仍由 `shouldShowSource` 裁决——渲染态用 replace + `LinkWidget`，源码态用 `cm-link-source`（Wiki 链接额外加 `cm-wikilink-source`）底色标记。
- **URL 安全**：`isSafeUrl` 黑名单拦截 `javascript:`/`vbscript:`/`data:text/html`（大小写与空白无关、精准拦截而非全禁 `data:`），`sanitizeUrl` 再过 `encodeURI` 并 `try/catch` 兜底；危险 URL 不设 `href`、文字仍显示但不可点。
- **Wiki 链接**走另一条安全通道：`href=''` + `preventDefault` + 应用层 `onWikiLinkClick` 回调，target 不进 DOM、不经净化。
- `update` 用「文档变 → 拖拽结束 → 拖拽中冻结 → 选区变」的顺序内联了与 `checkUpdateAction` 等价的调度逻辑（但并未调用它），拖拽期间冻结装饰防抖动。

## 7. 下一步学习建议

- 至此 u6「图片与链接」单元已完结。如果你还没读 u6-l1（imageField）、u6-l2（imageLoader），建议补齐——它们与本讲的 `shouldShowSource` 双模式、异步加载思路相互印证，且 `resolveImagePath` 的路径净化与本讲 `sanitizeUrl` 的协议净化合起来，构成完整的「用户输入 → 安全输出」图景。
- 进入 u7 单元：u7-l1（editorTheme）会解释本讲反复提到的 `cm-link-widget`、`cm-wikilink-widget` 等 CSS 类是如何由 HSL 变量定义的；u7-l3（整体架构）会把 ViewPlugin、StateField、Widget、Facet 的角色做一次全局归纳。
- 想动手练手：参考 u7-l4（自定义插件开发），尝试写一个**引用链接** `[text][ref]` + `[ref]: url` 的 Live Preview 插件，复用本讲的 `shouldShowSource`、`Decoration.replace` 与 `sanitizeUrl`，体会「解析 → 跳过 → 决策 → 净化 → 渲染」的完整开发流程。
