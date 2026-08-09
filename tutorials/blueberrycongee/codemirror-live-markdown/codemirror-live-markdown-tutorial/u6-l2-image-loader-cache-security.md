# imageLoader：异步加载、双缓存与路径安全

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 `loadImage` 如何用浏览器原生 `Image` 对象异步加载图片，并用 `setTimeout` 做超时保护；
- 理解「双缓存」——`imageCache`（成功结果缓存）与 `loadingPromises`（在途请求去重）——各自的生命周期与职责，并能解释为什么失败结果刻意不缓存；
- 掌握 `resolveImagePath` 对 `data:`、`http(s)`、相对路径三类输入的不同处理，以及它对 `../`、`./` 的净化逻辑与局限性；
- 能够复现并验证缓存命中、路径净化等行为。

本讲是图片子系统（单元 6）的第二讲。上一讲 [u6-l1](u6-l1-image-field-preview.md) 讲了 `imageField` 与 `ImageWidget` 如何把 `![alt](url)` 渲染成画面，其中 `ImageWidget` 调用了 `loadImage(src, { basePath }).then(...)`，但把「异步加载、缓存、路径安全」的细节全部委托给了本讲的 `imageLoader`。本讲就来拆解这层被隐藏的基础设施。

## 2. 前置知识

- **JavaScript Promise 与异步**：`loadImage` 返回一个 `Promise<LoadedImage>`，理解 `new Promise(executor)` 的 executor 会同步执行、`.then` 回调异步入队即可。
- **浏览器 `Image` 对象**：`new Image()` 创建一个离屏图片元素，给 `img.src` 赋值会触发浏览器发起请求，成功触发 `onload`、失败触发 `onerror`。它的 `width`/`height` 在 `onload` 后才有意义。
- **`Map` 数据结构**：本模块用两个模块级 `Map` 充当缓存，键都是「解析后的图片地址」字符串。
- **路径穿越（path traversal）的概念**：服务端场景里，`../../../etc/passwd` 这类输入可能让程序读到约定目录之外的文件。本讲的 `resolveImagePath` 就包含对 `../` 的清洗。

> 本讲不依赖 CodeMirror 的任何概念——`imageLoader` 是 `utils` 层的纯工具，零 CodeMirror 依赖，可以脱离编辑器独立使用、独立测试。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/utils/imageLoader.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts) | 本讲主角。导出 `loadImage`、`preloadImages`、`clearImageCache`、`resolveImagePath` 四个函数与 `LoadedImage`/`LoadImageOptions` 两个类型，内部维护两个模块级缓存 `Map`。 |
| [src/utils/\_\_tests\_\_/imageLoader.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts) | Vitest 测试。用 `MockImage` 替换 `globalThis.Image` 模拟异步加载、成功/失败/超时，是验证本讲所有行为的最佳参照。 |
| [src/widgets/imageWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts) | 调用方。`ImageWidget.toDOM` 里 `loadImage(src, { basePath }).then(...)` 一行就是本模块与编辑器唯一的衔接点。 |
| [src/index.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts) | 桶文件，把本模块的函数与类型 re-export 给使用者。 |

## 4. 核心概念与源码讲解

本讲按「自底向上」的顺序拆成四个最小模块：先讲被 `loadImage` 首先调用的 `resolveImagePath`（路径与安全），再讲 `loadImage` 主体（异步与超时），然后单独深挖贯穿其中的双缓存机制，最后讲两个缓存管理辅助函数 `preloadImages` 与 `clearImageCache`。

### 4.1 resolveImagePath：路径解析与安全净化

#### 4.1.1 概念说明

用户在 Markdown 里写的图片地址千差万别：可能是网络绝对地址 `https://...`、可能是内联 base64 的 `data:` URL、也可能是相对地址 `./pic.png` 或 `assets/x.png`。`resolveImagePath` 的职责是在真正发起加载之前，把这些异构输入统一成一个「最终请求地址」：

- `data:` 与 `http(s)` 绝对地址：原样返回，无需处理；
- 相对地址：若调用方提供了 `basePath`，则拼成 `basePath + src`，并对 `../`、`./` 做净化。

它是一个**纯函数**——无副作用、无 IO，给定相同输入永远产出相同输出，因此极易测试（测试文件里 `resolveImagePath` 的用例全是同步断言）。

#### 4.1.2 核心流程

```
输入 src, basePath
  ├─ src 以 'data:' 开头?        → 返回 src（原样）
  ├─ src 以 'http://'/'https://' 开头? → 返回 src（原样）
  ├─ 提供了 basePath?
  │     ├─ 是 → 清洗 src：去掉所有 '../'，再去掉开头的 './'
  │     │       规范化 basePath：保证以 '/' 结尾
  │     │       返回 normalizedBase + sanitizedSrc
  │     └─ 否 → 返回 src（原样，无法解析）
```

关键步骤是相对路径分支里的**两段清洗**：

1. `src.replace(/\.\.\//g, '')` —— 全局删除字符串里**所有**出现的字面子串 `../`；
2. `.replace(/^\.\//g, '')` —— 删除**开头**的 `./`。

随后保证 `basePath` 以 `/` 结尾（没有就补一个），再拼接。这样无论用户写 `./pic.png`、`pic.png` 还是 `../../pic.png`，最终都会被收拢到 `basePath/` 之下。

#### 4.1.3 源码精读

函数主体 [src/utils/imageLoader.ts:46-69](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L46-L69)：

```ts
export function resolveImagePath(src: string, basePath?: string): string {
  // Return data URL as-is
  if (src.startsWith('data:')) {
    return src;
  }
  // Return absolute URL as-is
  if (src.startsWith('http://') || src.startsWith('https://')) {
    return src;
  }
  // Relative paths need resolution
  if (basePath) {
    // Remove path traversal attacks
    const sanitizedSrc = src.replace(/\.\.\//g, '').replace(/^\.\//g, '');
    const normalizedBase = basePath.endsWith('/') ? basePath : basePath + '/';
    return normalizedBase + sanitizedSrc;
  }
  return src;
}
```

- [data: 短路](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L48-L50)：内联 base64 图片直接返回，避免被当成相对路径拼接出错。
- [http(s) 短路](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L52-L55)：远程图原样返回。
- [清洗 + 拼接](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L58-L66)：注意 `normalizedBase` 用三元表达式保证末尾斜杠，避免拼出 `/assetspic.png` 这种错误。

对应的测试断言清晰展示了行为，尤其路径穿越用例 [src/utils/\_\_tests\_\_/imageLoader.test.ts:233-240](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L233-L240)：

```ts
it('should prevent path traversal attacks', () => {
  expect(resolveImagePath('../../../etc/passwd', '/assets/')).toBe('/assets/etc/passwd');
  expect(resolveImagePath('../../secret.png', '/assets/')).toBe('/assets/secret.png');
});
```

`'../../../etc/passwd'` 经全局删除 `../` 后变成 `'etc/passwd'`，再拼上 `/assets/` 得到 `/assets/etc/passwd`——图片地址被强制锁定在 `basePath` 目录下。

> **诚实说明（局限性）**：这里的清洗是**字面子串删除**，不是真正的路径规范化（canonicalization）。例如 `./a/../b.png` 经处理会变成 `/assets/a/b.png`（`../` 被当普通子串删掉，并不会真的「抵消上一级目录」），这与操作系统对 `..` 的语义不同。此外，它能挡住常见 `../` 写法，但并非完备的防穿越方案。好在浏览器环境下 `new Image()` 的请求受同源策略约束、也不会读取本地文件系统，因此这层清洗更多是「把相对地址规整到约定目录之下」的正确性保障，风险面比服务端文件读取小得多。理解它的「做了什么」和「没做什么」，比把它当成铜墙铁壁更重要。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `resolveImagePath` 对三类输入的处理。

**操作步骤**（在仓库根目录的 Node / Vitest 环境里）：

1. 新建一个临时脚本或在 Vitest 里 `import { resolveImagePath } from '../imageLoader'`；
2. 对下面每一行调用并打印结果：

```ts
// 示例代码（非项目原有代码）
console.log(resolveImagePath('data:image/png;base64,iVBOR='));
console.log(resolveImagePath('https://example.com/a.png'));
console.log(resolveImagePath('./a.png', '/assets/'));
console.log(resolveImagePath('a.png', '/assets'));
console.log(resolveImagePath('../../secret.png', '/assets/'));
console.log(resolveImagePath('./a/../b.png', '/assets/')); // 观察「非真规范化」
```

**预期结果**（可由源码直接推导，亦可本地验证）：

| 输入 | 输出 |
| --- | --- |
| `data:image/png;base64,iVBOR=` | `data:image/png;base64,iVBOR=`（原样） |
| `https://example.com/a.png` | `https://example.com/a.png`（原样） |
| `('./a.png', '/assets/')` | `/assets/a.png` |
| `('a.png', '/assets')` | `/assets/a.png`（basePath 自动补斜杠） |
| `('../../secret.png', '/assets/')` | `/assets/secret.png`（穿越被清洗） |
| `('./a/../b.png', '/assets/')` | `/assets/a/b.png`（注意：不是 `/assets/b.png`） |

**需要观察的现象**：最后一行最能体现「子串删除而非真规范化」的特点。

#### 4.1.5 小练习与答案

1. **问**：`resolveImagePath('http://x/a.png', '/assets/')` 会返回什么？basePath 被用上了吗？
   **答**：返回 `http://x/a.png`。`http://` 前缀触发短路直接返回，根本不会进入 basePath 分支，所以 basePath 被忽略。

2. **问**：`resolveImagePath('../../secret.png', '/assets/')` 为什么变成 `/assets/secret.png`？
   **答**：全局正则 `/\.\.\//g` 删掉了全部 `../`，剩下 `secret.png`，再拼上末尾已带斜杠的 `/assets/`。

3. **问**：如果想给 `basePath = '/assets'`（无尾斜杠）拼 `image.png`，源码如何避免拼成 `/assetsimage.png`？
   **答**：`normalizedBase = basePath.endsWith('/') ? basePath : basePath + '/'` 这一行保证基底一定以 `/` 结尾。

### 4.2 loadImage 的异步加载与超时保护

#### 4.2.1 概念说明

`loadImage` 是本模块的核心。它接收一个图片地址（和可选配置），返回一个 `Promise<LoadedImage>`：成功时 `LoadedImage.loaded === true` 并带 `width/height`；失败时 `loaded === false` 并带 `error` 字符串。

它的内部机制是：先用上一节的 `resolveImagePath` 解析出最终地址，然后**创建一个浏览器原生 `Image` 对象**去真正加载。图片加载天然是异步的，且可能因为网络问题、地址错误而**永远不返回**——`onload`/`onerror` 都不触发。为此 `loadImage` 额外加了一道**超时保护**：超过约定时间（默认 10 秒）仍未完成，就主动判定失败。这让上游（`ImageWidget`）永远能拿到一个 settle 的 Promise，不会因为某张图「卡死」而让状态机停在 loading。

#### 4.2.2 核心流程

设超时阈值为 \( T \)（默认 \( T = 10000 \) 毫秒）。一次 `loadImage(src, options)` 的执行：

```
resolvedSrc = resolveImagePath(src, basePath)
  ├─ imageCache 命中?           → 立即 resolve(缓存值)         // 见 4.3
  ├─ loadingPromises 命中?      → 返回那个在途 Promise          // 见 4.3
  └─ 都没命中 → 创建新 Promise p：
        new Image() → img
        timeoutId = setTimeout(在 T ms 后 → handleError('timeout'), T)
        img.onload  = handleSuccess
        img.onerror = () => handleError('Image load failed')
        img.src = resolvedSrc        // 真正开始加载
        loadingPromises.set(resolvedSrc, p)
        return p
```

`handleSuccess` 与 `handleError` 内部都有同一个 `resolved` 守卫，确保「`onload` / `onerror` / 超时」三者无论谁先到，**只有第一个**能真正 resolve 这个 Promise，其余的会被 `if (resolved) return` 拦掉。这三者之间存在竞争关系：例如图片可能在超时定时器触发前的最后一刻才加载完成，必须靠守卫保证不重复 resolve、不重复清理。

#### 4.2.3 源码精读

函数签名与默认值 [src/utils/imageLoader.ts:78-82](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L78-L82)：

```ts
export function loadImage(src: string, options: LoadImageOptions = {}): Promise<LoadedImage> {
  const { timeout = 10000, basePath } = options;
```

`timeout = 10000` 是默认超时；`options` 默认 `{}` 保证不传也不报错。

成功分支 [src/utils/imageLoader.ts:113-127](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L113-L127) 把结果写入 `imageCache` 并 resolve：

```ts
const handleSuccess = () => {
  if (resolved) return;
  resolved = true;
  cleanup();
  const result: LoadedImage = { src: resolvedSrc, width: img.width, height: img.height, loaded: true };
  imageCache.set(resolvedSrc, result);   // 只有成功才缓存
  resolve(result);
};
```

失败分支 [src/utils/imageLoader.ts:129-144](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L129-L144)：

```ts
const handleError = (error: string) => {
  if (resolved) return;
  resolved = true;
  cleanup();
  const result: LoadedImage = { src: resolvedSrc, width: 0, height: 0, loaded: false, error };
  // Don't cache failed results, allow retry
  resolve(result);
};
```

注意两处关键差异：`handleError` 的 `width/height` 置 0、`loaded: false`、带 `error`；并且**没有** `imageCache.set(...)`——失败结果不进缓存（详见 4.3）。

超时与事件接线 [src/utils/imageLoader.ts:147-155](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L147-L155)：

```ts
timeoutId = setTimeout(() => {
  handleError(`Image load timeout after ${timeout}ms`);
}, timeout);
img.onload = handleSuccess;
img.onerror = () => handleError('Image load failed');
img.src = resolvedSrc;   // 赋值后才真正发起请求
```

`cleanup` [src/utils/imageLoader.ts:105-111](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L105-L111) 在成功/失败时各调一次，做两件事：清掉超时定时器（避免泄露）、从 `loadingPromises` 删除这条在途记录：

```ts
const cleanup = () => {
  if (timeoutId) { clearTimeout(timeoutId); timeoutId = null; }
  loadingPromises.delete(resolvedSrc);
};
```

超时行为有专门测试覆盖 [src/utils/\_\_tests\_\_/imageLoader.test.ts:79-103](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L79-L103)：它用一个**永不触发** onload/onerror 的 `SlowImage` 替换全局 `Image`，设 `timeout: 100`，再用 `vi.useFakeTimers()` + `advanceTimersByTime(150)` 快进时间，断言结果 `loaded === false` 且 `error` 包含 `'timeout'`。

#### 4.2.4 代码实践

**实践目标**：阅读超时测试，理解超时如何被确定性验证。

**操作步骤**：

1. 打开 [src/utils/\_\_tests\_\_/imageLoader.test.ts:79-103](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L79-L103)；
2. 关注三点：① `SlowImage` 没有 onload/onerror，模拟「永远不返回」；② `timeout: 100` 把阈值压到 100ms；③ `vi.advanceTimersByTime(150)` 人为推进 150ms 触发定时器。
3. 运行 `npm test -- imageLoader` 观察该用例通过。

**需要观察的现象**：即便真实图片永远不返回，`loadImage` 的 Promise 也会在超时后 settle，且 `result.error` 形如 `Image load timeout after 100ms`。

**预期结果**：测试通过，断言 `result.loaded` 为 `false`、`result.error` 含 `'timeout'`。

> 这个测试展示了「假定时器 + 永不完成的 Mock」这一套路——它是验证超时逻辑的标准写法，值得记下来。

#### 4.2.5 小练习与答案

1. **问**：`loadImage` 的默认超时是多少毫秒？
   **答**：10000ms（10 秒），来自 `const { timeout = 10000 } = options`。

2. **问**：为什么 `handleSuccess` 和 `handleError` 开头都要 `if (resolved) return;`？
   **答**：`onload`、`onerror`、超时定时器三者存在竞争，守卫确保只有最先到达的那一个能 resolve Promise 并执行 cleanup，其余被拦下，避免重复 resolve 与重复清理。

3. **问**：超时错误信息长什么样？测试用什么断言来匹配它？
   **答**：形如 `Image load timeout after ${timeout}ms`；测试用 `expect(result.error).toContain('timeout')` 匹配其中的 `'timeout'` 子串，避免硬编码具体毫秒数。

### 4.3 双缓存：imageCache 与 loadingPromises

#### 4.3.1 概念说明

`loadImage` 之所以高效，靠的是**两个模块级 `Map`** 协同：

- `imageCache: Map<string, LoadedImage>` —— **成功结果缓存**。长期存在，直到 `clearImageCache()` 被调用。它让「已经成功加载过的图」永远不必再发起请求。
- `loadingPromises: Map<string, Promise<LoadedImage>>` —— **在途请求去重**。短期存在，请求一旦 settle（无论成败）就被 `cleanup()` 删除。它让「同一时刻多次请求同一张图」只真正加载一次。

两者键都是 **`resolvedSrc`**（解析后的最终地址），而非用户原始 `src`——这保证了 `./a.png` 与 `a.png`（在相同 basePath 下解析到同一地址）能共享缓存。

这是两个**不同时间尺度**的优化：`loadingPromises` 处理「同一张图正被并发请求」的瞬间去重，`imageCache` 处理「同一张图历史上已加载过」的长期复用。

#### 4.3.2 核心流程

`loadImage` 在创建新请求之前，按优先级依次查两层缓存：

```
resolvedSrc = resolveImagePath(src, basePath)
hit = imageCache.has(resolvedSrc)        // 第一层：长期结果缓存
   || loadingPromises.has(resolvedSrc)    // 第二层：在途 Promise 缓存
```

可形式化为一次命中的判定：

\[
\text{hit}(s) = \text{imageCache.has}(s)\ \lor\ \text{loadingPromises.has}(s)
\]

命中后行为分两种：

| 缓存层 | 命中时返回 | 副作用 | 生命周期 |
| --- | --- | --- | --- |
| `imageCache` | `Promise.resolve(缓存值)` | 无（连 `new Image()` 都不创建） | 长期，直到 `clearImageCache()` |
| `loadingPromises` | 同一个在途 `Promise` | 无（共用首次创建的 `Image`） | 短期，settle 后 `cleanup()` 删除 |

**为什么失败结果不缓存**：`handleError` 没有 `imageCache.set(...)`。这是刻意设计——网络故障、地址写错常常是**临时性**的；若把失败结果也缓存，一次偶发失败就会让这张图「永久判死刑」，即使用户修正了地址或网络恢复也无法重试。不缓存失败 → 下次 `loadImage` 时 `imageCache` 不命中 → 重新创建 `Image` 发起请求 → 允许重试。源码注释明确写了这一点 [src/utils/imageLoader.ts:142-143](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L142-L143)：

```ts
// Don't cache failed results, allow retry
```

#### 4.3.3 源码精读

两个缓存声明 [src/utils/imageLoader.ts:33-37](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L33-L37)：

```ts
// Image cache
const imageCache = new Map<string, LoadedImage>();
// Loading promise cache (prevent duplicate requests)
const loadingPromises = new Map<string, Promise<LoadedImage>>();
```

`loadImage` 开头的两层查找 [src/utils/imageLoader.ts:84-97](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L84-L97)：

```ts
const resolvedSrc = resolveImagePath(src, basePath);
// Check cache
const cached = imageCache.get(resolvedSrc);
if (cached) {
  return Promise.resolve(cached);   // 长期命中：立即返回，不发请求
}
// Check if already loading
const loading = loadingPromises.get(resolvedSrc);
if (loading) {
  return loading;                    // 在途命中：共用同一个 Promise
}
```

两层都没命中才创建新 Promise，并在返回前把它登记进 `loadingPromises` [src/utils/imageLoader.ts:158-159](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L158-L159)：

```ts
loadingPromises.set(resolvedSrc, promise);
return promise;
```

登记发生在 `new Promise(...)` **之后**：promise 的 executor 是同步执行的（里面已经 `new Image()`、`setTimeout`、`img.src=...`），但因为图片加载是异步的，登记完成前不会有别的调用能命中它；登记完成后，紧接着任何并发调用都会在第二层命中。

**两层缓存各有一个针对性测试**：

- 长期缓存：[should return cached result for same URL](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L161-L183)。首次加载成功后，把全局 `Image` 换成「必定失败」的 `FailImage`，再次 `loadImage` 同一地址——结果仍是 `loaded: true`，证明第二次**根本没新建 Image**，直接吃了缓存。
- 在途去重：[should deduplicate same URLs](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L139-L150)（在 `preloadImages` 分组里）。同一地址传三次，三次返回的结果 `toEqual` 完全相等，因为它们复用了同一个在途 Promise。

#### 4.3.4 代码实践

**实践目标**：用「计数构造次数」的方式，亲手证明第二次调用命中 `imageCache`、没有新建 `Image`。

**操作步骤**（仿照现有测试风格，向 `imageLoader.test.ts` 临时新增一个用例）：

```ts
// 示例代码（非项目原有代码，用于练习）
it('should construct Image only once when cache hits', async () => {
  let constructCount = 0;
  class CountingImage {
    src = '';
    width = 0;
    height = 0;
    onload: (() => void) | null = null;
    onerror: ((e: Error) => void) | null = null;
    constructor() {
      constructCount++;           // 每次 new Image() 都自增
      setTimeout(() => {
        this.width = 800; this.height = 600; this.onload?.();
      }, 10);
    }
  }
  (globalThis as unknown as { Image: typeof CountingImage }).Image = CountingImage;

  await loadImage('https://example.com/once.png');   // 首次：constructCount = 1
  await loadImage('https://example.com/once.png');   // 第二次：应命中缓存
  expect(constructCount).toBe(1);                    // 只构造了一次
});
```

**需要观察的现象**：两次 `await loadImage` 后 `constructCount` 仍为 1，说明第二次走的是 `imageCache.get` 命中分支（`return Promise.resolve(cached)`），从未进入 `new Promise` 内部，自然不会 `new Image()`。

**预期结果**：`constructCount === 1`，断言通过。（待本地验证：可运行 `npm test -- imageLoader`。）

**延伸**：若把两次调用改成**并发**（不 `await` 第一次，直接 `Promise.all([loadImage(x), loadImage(x)])`），`constructCount` 同样应为 1——这次命中的是 `loadingPromises` 而非 `imageCache`。可以自己加一个用例对照。

#### 4.3.5 小练习与答案

1. **问**：`imageCache` 与 `loadingPromises` 的生命周期有何不同？
   **答**：`imageCache` 长期存在，只有 `clearImageCache()` 会清空；`loadingPromises` 是短期的，每条记录在请求 settle 时由 `cleanup()` 删除，只覆盖「在途」这段时间。

2. **问**：为什么失败结果不写进 `imageCache`？
   **答**：为了允许重试。失败往往是临时的（网络波动、地址拼错），不缓存失败结果，下次调用就不会命中、会重新发起请求，给恢复/修正留出机会。

3. **问**：两层缓存的键为什么用 `resolvedSrc` 而不是原始 `src`？
   **答**：因为最终发起请求的 URL 是 `resolvedSrc`。用解析后的地址作键，才能让 `./a.png` 与 `a.png`（相同 basePath 下）正确命中同一张图，缓存与真实请求一一对应。

### 4.4 缓存管理：preloadImages 与 clearImageCache

#### 4.4.1 概念说明

围绕两个缓存，模块还提供两个辅助函数：

- `preloadImages(srcs, options)`：**并行**预加载多张图，就是对每个地址调一次 `loadImage` 再 `Promise.all`。受益于 4.3 的去重，重复地址自动合并。
- `clearImageCache()`：清空 `imageCache`，强制后续重新加载。常用于「图片资源可能已更新、想强制刷新」的场景。

#### 4.4.2 核心流程

```
preloadImages(srcs, options):
  promises = srcs.map(src => loadImage(src, options))   // 并发，重复 src 自动去重
  return Promise.all(promises)

clearImageCache():
  imageCache.clear()   // 注意：只清 imageCache，不清 loadingPromises
```

#### 4.4.3 源码精读

`preloadImages` [src/utils/imageLoader.ts:169-176](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L169-L176)：

```ts
export async function preloadImages(
  srcs: string[],
  options: LoadImageOptions = {}
): Promise<LoadedImage[]> {
  const promises = srcs.map((src) => loadImage(src, options));
  return Promise.all(promises);
}
```

`clearImageCache` [src/utils/imageLoader.ts:181-183](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L181-L183)：

```ts
export function clearImageCache(): void {
  imageCache.clear();
}
```

> **一个值得注意的细节**：`clearImageCache()` 只清 `imageCache`，**不清** `loadingPromises`。这意味着若某张图此刻正处在途加载中，调用 `clearImageCache()` 不会打断它——它会照常完成，成功时还会重新写入 `imageCache`。绝大多数场景下没问题（测试 [should clear cache on demand](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L185-L205) 里首次加载已完成、在途记录已 cleanup，所以清空后立刻能重载）。但如果你依赖「调用后立即强制全部重新拉取」，要意识到正在途中的那批不受影响。

「清空后重载」的测试 [src/utils/\_\_tests\_\_/imageLoader.test.ts:185-205](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L185-L205)：先加载成功（进缓存）→ `clearImageCache()` → 把 `Image` 换成必失败的 `FailImage` → 再次加载，结果变回 `loaded: false`，证明缓存确被清空、确实重新发起了请求。

#### 4.4.4 代码实践

**实践目标**：观察 `preloadImages` 对重复地址的自动去重。

**操作步骤**：

1. 阅读 [should deduplicate same URLs](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L139-L150)；
2. 结合 4.3.4 的计数思路，给 `preloadImages` 写一个用例：传入三个相同地址，断言 `constructCount === 1`、返回数组长度为 3 且三项 `toEqual` 相等。

**预期结果**：`Image` 只构造一次；`Promise.all` 仍返回长度 3 的数组，三项内容完全相同（都来自同一个在途 Promise 的 resolve 值）。

#### 4.4.5 小练习与答案

1. **问**：`preloadImages` 如何实现「并行」？
   **答**：`srcs.map(src => loadImage(src, options))` 一次性把所有 `loadImage` 都启动（它们返回的 Promise 立即在途），再用 `Promise.all` 等全部完成，所以是并发而非串行。

2. **问**：`clearImageCache()` 会清掉 `loadingPromises` 吗？会有什么影响？
   **答**：不会。正在途中的加载会继续完成，成功时还会重新填回 `imageCache`，所以「在途那批」不受清空影响。

3. **问**：`preloadImages(['a','a','a'])` 会发起几次真实请求？为什么？
   **答**：一次。第一个 `loadImage('a')` 创建在途 Promise 并登记进 `loadingPromises`；第二、三个调用命中在途去重，复用同一个 Promise。

## 5. 综合实践

把本讲四个模块串起来，跟踪一个真实场景：「同一份文档里同一张相对路径图片被渲染了两次，其中一次加载还失败了」。

**任务**：写一段话 + 一张状态表，描述下列流程中每一步 `resolveImagePath`、`loadImage`、`imageCache`、`loadingPromises` 的状态变化。

假设 `basePath = '/assets/'`，文档里两处都写 `![](./logo.png)`，编辑器渲染时 `ImageWidget` 会调用 `loadImage('./logo.png', { basePath })`。

1. 第一处渲染触发 `loadImage('./logo.png', { basePath })`：
   - `resolveImagePath` 把它解析成什么？
   - `imageCache` / `loadingPromises` 命中情况？是否 `new Image()`？`loadingPromises` 是否新增条目？
2. 紧接着（第一处还没加载完）第二处渲染又触发一次 `loadImage('./logo.png', { basePath })`：
   - 这次命中哪一层缓存？是否再次 `new Image()`？
3. 假设这次加载失败（`onerror`）：
   - `handleError` 执行了什么？`cleanup` 删掉了哪个 Map 的条目？`imageCache` 是否新增？为什么？
4. 用户稍后重新触发渲染，第三次 `loadImage`：
   - 此时 `imageCache` 命中吗？为什么还能重试？`new Image()` 会再次发生吗？

**参考答案要点**：

| 步骤 | resolvedSrc | imageCache | loadingPromises | new Image() |
| --- | --- | --- | --- | --- |
| 1 首次 | `/assets/logo.png` | 无 | 新增一条 | 是（1 次） |
| 2 并发第二次 | `/assets/logo.png` | 无 | **命中**，复用 | 否 |
| 3 失败 settle | `/assets/logo.png` | **不写入**（失败不缓存） | cleanup 删除该条 | —— |
| 4 重试 | `/assets/logo.png` | 未命中（因 3 没缓存） | 重新新增一条 | 是（重新加载，允许重试） |

关键结论：相对路径先被 `resolveImagePath` 收拢成 `/assets/logo.png`；并发请求靠 `loadingPromises` 去重、只加载一次；失败靠「不缓存」保留重试能力；成功才会进 `imageCache` 供长期复用。

## 6. 本讲小结

- `resolveImagePath` 是纯函数，对 `data:` / `http(s)` 原样返回，对相对路径按 `basePath` 拼接，并用「全局删 `../` + 删开头 `./`」做基本净化——但这是子串删除，非真正的路径规范化。
- `loadImage` 用原生 `Image` 异步加载，靠 `setTimeout` 提供超时保护，靠 `resolved` 守卫让 onload/onerror/超时三者只有最先到达的生效，保证 Promise 一定会 settle。
- 双缓存是性能核心：`imageCache`（长期、存成功结果）+ `loadingPromises`（短期、在途去重），键统一用 `resolvedSrc`。
- 失败结果**刻意不缓存**，从而允许临时故障后重试；这是「正确性 vs 性能」的一次明确取舍，偏向正确性。
- `preloadImages` 用 `map + Promise.all` 并发预加载，重复地址自动去重；`clearImageCache` 只清 `imageCache`，不影响在途请求。
- 整个模块零 CodeMirror 依赖，是 `utils` 层可独立测试、独立复用的基础设施；上游 `ImageWidget` 只消费它的成败契约。

## 7. 下一步学习建议

- 下一讲 [u6-l3](u6-l3-link-plugin-and-security.md) 转向**链接**：`linkPlugin` 如何渲染标准链接与 Wiki 链接，以及 `sanitizeUrl`/`isSafeUrl` 对 `javascript:` 等危险协议的拦截——与本讲的 `resolveImagePath` 路径净化遥相呼应，都属于「输入净化」主题，建议对照阅读。
- 若想再深入本模块，可思考两个改进方向并尝试实现：① `resolveImagePath` 改用更稳健的 URL 规范化（如 `new URL(src, base)`）；② `clearImageCache` 同时处理在途请求（需评估对正在渲染的 Widget 的影响）。
- 推荐回顾上一讲 [u6-l1](u6-l1-image-field-preview.md) 的 `ImageWidget.toDOM` 状态机，结合本讲确认：widget 只用 `loadImage` 的 `loaded/error` 决定换 `<img>` 还是换错误占位，缓存对它完全透明——这正是良好分层的体现。
