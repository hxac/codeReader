# 跑起来再说：测试与基准初探

## 1. 本讲目标

前两讲（u1-l1、u1-l2）我们建立了两个认知：gpui_wgpu 是「基于 wgpu 的渲染器 + 精灵图集 + cosmic-text 文本系统」，它的代码组织是「4 个私有模块 + 门面式再导出 + 4 个 WGSL 文件经 `include_str!` 拼成三种着色器变体」。本讲换一个视角——**不读功能代码，读「验证功能代码的代码」**。测试和基准是理解一个陌生 crate 最快的捷径：每个测试都是一个「作者亲自写的、带断言的最小使用示例」，它告诉你这段代码的输入是什么、预期行为是什么、曾经在哪里出过 bug。

学完本讲，你应该能：

1. 会运行本 crate 的全部测试与基准：`cargo test -p gpui_wgpu` 与 `cargo bench -p gpui_wgpu layout_line`，并会用子串过滤只跑某一族测试。
2. 能准确区分**纯 CPU 测试**（28 个：着色器静态校验、排版、回退链、字节序交换……）和**需要真实 GPU 适配器的测试**（2 个：图集回归测试），并知道无 GPU 环境下哪些失败属于环境限制而非代码回归。
3. 理解 `layout_line` 基准的语料设计：为什么 `code_text()` 要把换行替换成空格、`text_mixed_direction` 又额外测了什么。
4. 初步建立「改了代码 → 跑哪些测试」的反射，为后续每一讲的源码实验铺好验证手段。

## 2. 前置知识

本讲涉及的 Rust 测试与文本概念，先用大白话过一遍：

- **`#[cfg(test)] mod tests` 惯例**：Rust 的单元测试直接住在源码文件末尾，用一个仅在测试编译（`cargo test`）时才存在的模块包起来。平时 `cargo build` 根本不会编译它们，零运行时开销。这与 Java/JUnit「测试放独立目录」的风格不同，好处是测试紧挨着被测代码。
- **`cargo test` 的过滤参数**：`cargo test -p gpui_wgpu <子串>` 只运行完整路径（如 `wgpu_atlas::tests::xxx`）中包含该子串的测试。本讲的分类实践全靠它。
- **dev-dependencies**：只在编译测试与基准时生效的依赖。本 crate 的两个 dev-dependency 很能说明问题：`criterion`（基准框架）和 `naga`（着色器校验器，见 [Cargo.toml:51-53](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L51-L53)）——测试设施本身就是本 crate 技术栈的缩影。
- **naga**：wgpu 内部用来把 WGSL（WebGPU Shading Language）翻译成各平台着色器语言的组件。关键点：**WGSL 是运行时才编译的字符串**，rustc 对它不做任何检查；把 naga 作为 dev-dependency 引入后，可以在测试里于 CPU 上「预编译」这些字符串，提前暴露语法与类型错误。
- **回归测试（regression test）**：先有一个真实 bug，修复后把「能触发旧 bug 的最小场景」固化成测试，防止它悄悄回来。本讲会看到两个带注释的活例子。
- **criterion 基准**：Rust 最常用的统计型基准框架，对一个函数反复计时并给出分布（中位数、离群值等）。`[[bench]] harness = false` 表示不用标准库的测试 harness，由 `criterion_main!` 自己提供 `main`。
- **双向文本（BiDi）与段落分隔符**：希伯来文、阿拉伯文从右往左排（RTL），英文从左往右（LTR），同一段文字里混排就是双向文本。Unicode 定义了一类「段落分隔符」（`Bidi_Class=B` 的字符，如 `\n`、`\r`、`U+001C`、`U+2029`）——每个分隔符都开启一个新的双向段落，排版方向可以不同。**记住这条：`\n` 本身就是段落分隔符**，它是本讲基准语料设计的核心伏笔。
- **POD 与 `std::mem::size_of`**：POD（plain old data）指可以按裸字节复制的结构体（本 crate 用 bytemuck 做这件事）。`size_of::<T>()` 返回其字节大小。当 Rust 结构体要被 GPU 着色器按「N 个 32 位 word」逐字读取时，两侧的字节数必须严格相等——本讲的第 4 个测试就是干这个的。

## 3. 本讲源码地图

以当前 HEAD（`fa00dccc`）统计，本 crate 共有 **30 个单元测试**，分布在 4 个源码文件里；另有 1 个 criterion 基准目标。先给一张总分类表——它就是本讲的骨架：

| 位置 | 数量 | 需要 GPU？ | 测什么 |
| --- | --- | --- | --- |
| [src/wgpu_renderer.rs:2201-2243](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2201-L2243) | 4 | ❌ 纯 CPU | 3 个 naga WGSL 静态校验 + 1 个「Rust 结构体大小 = WGSL word 数 × 4」断言 |
| [src/wgpu_atlas.rs:401-527](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L401-L527) | 4 | ⚠️ 2 个需要、2 个纯 CPU | 图集分配/上传的两条 GPU 回归测试 + 两条字节序交换的纯 CPU 测试 |
| [src/cosmic_text_system.rs:996-1409](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L996-L1409) | 21 | ❌ 纯 CPU | 段落分隔符排版、字形索引有序性、字体回退链划分 |
| [src/wgpu_context.rs:585-606](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L585-L606) | 1 | ❌ 纯 CPU | `parse_pci_id` 十六进制解析（u2 单元细讲，此处仅归类） |
| [benches/layout_line.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/benches/layout_line.rs#L1-L104) | 基准 | ❌ 纯 CPU | `layout_line` 三种场景的每行排版耗时 |

运行方式（在 zed 仓库根目录）：

```bash
cargo test -p gpui_wgpu            # 全部 30 个单元测试
cargo bench -p gpui_wgpu layout_line   # 基准（首次需编译 gpui/wgpu，耗时较长）
```

顺带一提：Zed 的 CI 用 `cargo nextest run --workspace` 跑全仓库测试（见 [run_tests.yml:360](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/.github/workflows/run_tests.yml#L360)）；本讲为简单起见统一用 `cargo test`，本地装了 nextest 的话效果等价。

## 4. 核心概念与源码讲解

### 4.1 wgpu_renderer 的测试：naga WGSL 校验与 POD 大小断言

#### 4.1.1 概念说明

回忆 u1-l2 的结论：四个 WGSL 文件不是 Rust 模块，而是在编译期被 `include_str!` + `concat!` 拼成三个字符串常量。这带来一个独特的质量问题——**着色器代码没有编译器把关**。Rust 代码写错一个分号，`cargo check` 立刻报错；WGSL 写错一个分号，要等到运行时创建渲染管线、wgpu 调 naga 编译它时才会 panic。对 GPU 渲染库来说，这个反馈太迟了。

本模块的解法非常优雅：既然 wgpu 内部用 naga 编译 WGSL，那就把 naga 直接列为 dev-dependency，在测试里对拼接好的三个字符串常量做「CPU 侧预编译」。不需要 GPU、不需要窗口，几毫秒就能确认所有着色器变体语法与类型合法。

此外还有第二个隐患：实例数据（Quad、Shadow、Sprite 等 POD 结构体）按裸字节写给 GPU，而 WebGL 变体的着色器按「每实例 N 个 32 位 word」定位记录。**Rust 侧加一个字段、WGSL 侧忘了改，编译照样通过，渲染只会静默错乱**——这是最难查的一类 bug。于是有了第 4 个测试：用 `size_of` 断言把「两侧约定」变成硬约束。

#### 4.1.2 核心流程

```text
编译期（u1-l2 已讲）:
  shaders.wgsl + shaders_storage.wgsl    ──concat!──▶ STORAGE_BUFFER_SHADERS
  shaders.wgsl + shaders_webgl.wgsl      ──concat!──▶ WEBGL_SHADERS
  "enable dual_source_blending;\n"
    + shaders.wgsl + shaders_storage.wgsl
    + shaders_subpixel.wgsl              ──concat!──▶ SUBPIXEL_SHADERS

测试期（本讲）:
  cargo test
    └─ tests::validate_wgsl(变体, 能力集)
         ├─ naga::front::wgsl::parse_str(source)     # 词法/语法解析
         └─ Validator::new(全部校验标志, 能力集)
              .validate(&module)                       # 类型/绑定/控制流校验
```

「能力集」（`naga::valid::Capabilities`）是这组测试的第二个维度：传**空能力集**等于告诉 naga「目标平台什么都不支持」，着色器一旦用到高级特性就会校验失败——这正好模拟最保守的 WebGL2 目标；而亚像素变体用到双源混合，必须显式传 `DUAL_SOURCE_BLENDING` 才合法。

#### 4.1.3 源码精读

三个待校验的常量在文件开头定义，测试在文件末尾，首尾呼应：

- [src/wgpu_renderer.rs:21-43](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L21-L43) — 三个着色器变体常量：storage buffer 变体（原生平台）、WebGL 变体（WebGL2 无 storage buffer，改用纹理传输实例）、亚像素变体（`enable dual_source_blending;` 指令必须在所有声明之前，所以拼在最前面）。

测试模块本体：

- [src/wgpu_renderer.rs:2206-2210](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2206-L2210) — `webgl_shader_is_valid_wgsl_without_storage_buffers`：先用字符串断言 `!WEBGL_SHADERS.contains("var<storage")` 确保 WebGL 变体没混进 storage buffer，再用空能力集校验。双保险。

```rust
#[test]
fn webgl_shader_is_valid_wgsl_without_storage_buffers() {
    assert!(!WEBGL_SHADERS.contains("var<storage"));
    validate_wgsl(WEBGL_SHADERS, naga::valid::Capabilities::empty());
}
```

- [src/wgpu_renderer.rs:2212-2215](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2212-L2215) — `storage_buffer_shader_is_valid_wgsl`：原生变体，同样空能力集校验。
- [src/wgpu_renderer.rs:2217-2223](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2217-L2223) — `subpixel_shader_is_valid_wgsl`：唯一一个需要传入 `Capabilities::DUAL_SOURCE_BLENDING` 的测试——能力集在这里从「平台模拟」升级为「变体差异的显式声明」。
- [src/wgpu_renderer.rs:2225-2230](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2225-L2230) — 辅助函数 `validate_wgsl`：两行核心——`parse_str` 解析、`Validator` 校验，任何一步失败 `expect` 都会让测试带详细错误信息地失败。

```rust
fn validate_wgsl(source: &str, capabilities: naga::valid::Capabilities) {
    let module = naga::front::wgsl::parse_str(source).expect("shader should parse");
    naga::valid::Validator::new(naga::valid::ValidationFlags::all(), capabilities)
        .validate(&module)
        .expect("shader should validate");
}
```

- [src/wgpu_renderer.rs:2232-2242](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2232-L2242) — `webgl_record_sizes_match_shader_word_strides`：断言 8 种实例记录的字节大小。`Quad`、`Shadow`、`Underline`、`MonochromeSprite`、`SubpixelSprite`、`PolychromeSprite` 来自 gpui（见 [src/wgpu_renderer.rs:2204](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2204) 的 use 声明），`PathRasterizationVertex` 与 `PathSprite` 是本 crate 私有类型（定义于 [src/wgpu_renderer.rs:99-105](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L99-L105)）。

```rust
assert_eq!(std::mem::size_of::<Quad>(), 40 * 4);
assert_eq!(std::mem::size_of::<Shadow>(), 28 * 4);
// …共 8 条，PathSprite 最小（4 word = 16 字节），Quad 最大（40 word = 160 字节）
```

这些数字不是凭空约定的——WGSL 侧用同样的系数定位每条记录，例如 quad 着色器取 `instance_cursor(instance_id * 40u)`、underline 取 `* 16u`、mono sprite 取 `* 28u`、path 光栅化顶点取 `vertex_id * 26u`（见 [src/shaders_webgl.wgsl:139-210](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/shaders_webgl.wgsl#L139-L210)）；每个纹元（texel）打包 4 个 word 的约定写在该文件头部注释里（[src/shaders_webgl.wgsl:3-10](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/shaders_webgl.wgsl#L3-L10)）。换算关系：

\[ \text{Rust 侧字节} = \text{WGSL 侧 word 数} \times 4 \]

#### 4.1.4 代码实践

**实践目标**：确认着色器校验测试在无 GPU 环境即可运行，并亲身体验「WGSL 错误在测试期暴露」的反馈环。

**操作步骤**：

1. 在 zed 仓库根目录运行三个校验测试（子串过滤，三条等价于一条 `cargo test -p gpui_wgpu wgpu`）：
   ```bash
   cargo test -p gpui_wgpu shader_is_valid
   cargo test -p gpui_wgpu webgl_record_sizes
   ```
2. 实验环节（在自己的本地克隆里做，**做完务必还原**）：打开 `crates/gpui_wgpu/src/shaders_storage.wgsl`，任意删掉一个分号，再运行：
   ```bash
   cargo test -p gpui_wgpu storage_buffer_shader_is_valid
   ```
3. 用 `git diff` 确认改动，然后 `git checkout -- crates/gpui_wgpu/src/shaders_storage.wgsl` 还原。

**需要观察的现象**：第 2 步中测试应当失败，且失败信息来自 naga 解析器，**带出错的行号和列号**（形如 `expected ';' found ...`），而不是运行时的 GPU panic。

**预期结果**：第 1 步 4 个测试全部通过（待本地验证）；第 2 步测试失败并精确定位到被删分号的那一行（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `webgl_shader_is_valid_wgsl_without_storage_buffers` 要在 `validate_wgsl` 之前先做一个 `!contains("var<storage")` 的字符串断言？校验器本身不能发现吗？

**答案**：字符串断言是「源头防御」：WebGL2 后端根本没有 storage buffer，如果有人把 storage 相关代码错误地拼进 WebGL 变体，空能力集校验可能会捕获，但报错信息会是隐晦的类型/能力错误；而 `contains` 断言直接指出「变体里出现了 storage 声明」这一根源，定位成本低一个数量级。两道防线各有分工。

**练习 2**：假如有人在 gpui 的 `Quad` 结构体里新增了一个 `f32` 字段，但忘了同步 `shaders_webgl.wgsl`，哪个测试会失败？如果只跑 `cargo check` 或启动 Zed，能发现吗？

**答案**：`webgl_record_sizes_match_shader_word_strides` 会失败，因为 `size_of::<Quad>()` 变成 41×4 字节而断言仍是 40×4。`cargo check` 完全发现不了（Rust 结构体本身合法）；启动 Zed 后实例数据与着色器读取错位，渲染结果静默错乱而不会必然报错——这正是该测试存在的意义。

**练习 3**：三个校验测试中，为什么唯独 `subpixel_shader_is_valid_wgsl` 要传 `Capabilities::DUAL_SOURCE_BLENDING`？

**答案**：SUBPIXEL_SHADERS 变体以 `enable dual_source_blending;` 开头并使用双源混合（第二个片元输出做逐通道 alpha，详见 u4-l4）。naga 校验时若能力集不含该项会判定着色器使用了目标平台不支持的能力而报错，所以必须显式声明。其余两个变体面向所有平台（尤其 WebGL2），用空能力集等价于「在最保守的目标上也能通过」。

### 4.2 wgpu_atlas 的测试：需要真实 GPU 的回归测试

#### 4.2.1 概念说明

`WgpuAtlas`（精灵图集，u1-l1 已建立概念：etagere 装箱 + GPU 纹理页）是本 crate 中唯一「测试也要真 GPU」的模块。原因很直接：图集一半是 CPU 数据结构（哪些 key 对应哪个 tile、分配器状态），一半是真 GPU 资源（`wgpu::Texture`、`write_texture` 上传）。后者没有 device/queue 就无法创建，而 `wgpu::Device` 无法伪造。

于是这个文件里的 4 个测试分成泾渭分明的两类：

- **2 条纯 CPU 测试**：只测 `swizzle_upload_data` 这个字节交换函数，输入输出都是 `Vec<u8>`，任何机器都能跑。
- **2 条 GPU 回归测试**：通过 `test_device_and_queue` 辅助函数请求真实 adapter/device/queue。两条都源于真实 bug——测试注释里白纸黑字写着 *"Regression test: before the fix, this panicked in flush_uploads"*。

注意模块门控是 `#[cfg(all(test, not(target_family = "wasm")))]`（[src/wgpu_atlas.rs:401-402](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L401-L402)）：这些测试只在原生目标编译，wasm 目标直接跳过。

#### 4.2.2 核心流程

先理解被测的时序，才能理解回归测试在防什么：

```text
第 N 帧:
  get_or_insert_with(key, build)     # CPU: 查缓存/分配 tile，把像素数据排入 pending_uploads
  remove(key)                        # CPU: 引用计数归零 → 释放 tile、可能回收纹理页
  before_frame()                     # 帧边界: flush_uploads() 把 pending 数据真正 write_texture
    └─ flush_uploads():
         for upload in pending_uploads.drain(..):
             let Some(texture) = storage.get(upload.id) else { continue }   # ← 回归修复点
             queue.write_texture(...)
```

隐患在于 `get_or_insert_with` 与 `before_frame` 之间存在一个窗口：如果 tile 在上传排队之后、刷新之前被 `remove` 掉，`flush_uploads` 就会拿着一个指向已不存在纹理的上传任务。修复前的代码在这里 panic。

#### 4.2.3 源码精读

- [src/wgpu_atlas.rs:408-440](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L408-L440) — `test_device_and_queue`：用 `Backends::all()` + `LowPower` 请求适配器（不绑定 surface），再以下限默认值请求 device/queue。**无 GPU 环境下 `request_adapter` 失败，函数返回 `Err`，调用它的测试随之失败**——这是环境限制，不是代码回归。注意它请求的是最保守的配置（空 features、downlevel limits），保证在尽可能多的机器上可用。

```rust
let adapter = instance
    .request_adapter(&wgpu::RequestAdapterOptions {
        power_preference: wgpu::PowerPreference::LowPower,
        compatible_surface: None,
        force_fallback_adapter: false,
    })
    .await
    .map_err(|error| anyhow::anyhow!("failed to request adapter: {error}"))?;
```

- [src/wgpu_atlas.rs:442-464](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L442-L464) — 回归测试一 `before_frame_skips_uploads_for_removed_texture`：插入一张 1×1 图片 tile → 立刻 `remove` → 调 `before_frame()`。注释明确说明修复前会在 `flush_uploads` panic。它防住的那行防御代码就是 [src/wgpu_atlas.rs:261-265](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L261-L265) 里的 `let Some(texture) = ... else { continue }`——纹理没了就跳过该条上传，而不是 panic。`before_frame` 的入口只有一行，见 [src/wgpu_atlas.rs:72-75](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L72-L75)。

- [src/wgpu_atlas.rs:466-508](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L466-L508) — 回归测试二 `remove_deallocates_tile_space_for_reuse`：先插入 64×64 小图与 700×700 大图，断言两者落在**同一张图集纹理页**（`texture_id` 相等）；移除大图后再插入另一张 700×700，断言它仍落在小图所在的同一页。这条测试验证 `remove`（[src/wgpu_atlas.rs:130](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L130)）真的把空间还给了 etagere 分配器供后续复用，而不是只删了索引、空间永久泄漏——泄漏的后果就是图集页无限增长。

- [src/wgpu_atlas.rs:510-517](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L510-L517) 与 [src/wgpu_atlas.rs:519-526](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L519-L526) — 两条纯 CPU 测试：`swizzle_upload_data_preserves_bgra_uploads`（目标纹理是 `Bgra8Unorm` 时字节原样通过）与 `swizzle_upload_data_converts_bgra_to_rgba`（目标是 `Rgba8Unorm` 时每个像素交换 R 与 B 通道：`10 20 30 40` 变 `30 20 10 40`）。被测函数 `swizzle_upload_data` 位于 [src/wgpu_atlas.rs:388](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L388)。

#### 4.2.4 代码实践

**实践目标**：按「是否需要真实 GPU 适配器」给本 crate 测试分类，并实际运行验证分类的正确性（对应总实践任务的第一部分）。

**操作步骤**：

1. 先跑纯 CPU 的那部分，任何机器都应通过：
   ```bash
   cargo test -p gpui_wgpu swizzle_upload_data
   ```
2. 再跑整个图集模块（子串 `atlas` 会命中 `wgpu_atlas::tests::*` 全部 4 条）：
   ```bash
   cargo test -p gpui_wgpu atlas
   ```
3. 把结果按「预期通过 / 因无 GPU 失败」两栏记录下来。若第 2 步有失败，阅读失败输出中的错误串：来自 `test_device_and_queue` 的会包含 `failed to request adapter`。

**需要观察的现象**：有 GPU 的机器上 4 条全绿；无 GPU（如某些 CI 容器、纯虚拟机）的环境里，`before_frame_skips_uploads_for_removed_texture` 与 `remove_deallocates_tile_space_for_reuse` 两条失败且错误信息指向适配器请求，而两条 swizzle 测试依旧通过。

**预期结果**：待本地验证。若你所在环境无 GPU，请把两条 GPU 测试的失败标注为「环境限制（无适配器），非代码回归」——这正是分类练习的意义：**失败信息里出现 `failed to request adapter` ≠ 代码坏了**。

#### 4.2.5 小练习与答案

**练习 1**：`test_device_and_queue` 请求 device 时为什么用 `wgpu::Limits::downlevel_defaults()` 而不是 `wgpu::Limits::default()`？

**答案**：`downlevel_defaults()` 是一组「低端 GPU 也能满足」的保守资源上限。测试用保守上限创建设备，可以保证测试在集显、虚拟机、老旧 GPU 上都能拿到设备，测试的关注点（图集逻辑）才不会被「这台机器上限不够」抢戏。这与本 crate 生产代码在 wasm/WebGL2 路径用 `downlevel_webgl2_defaults` 的思路一致：按最保守目标设计。

**练习 2**：为什么 `remove_deallocates_tile_space_for_reuse` 要构造「64×64 + 700×700」一大一小两个尺寸，而不是插入两张一样大的图？

**答案**：因为断言的核心是**纹理页身份**（`texture_id` 相等）：先证明小图和大图能共存于同一页（页内还有空间），移除大图后新大图仍能回到这一页。若两张图一样大，无法区分「复用了被释放的空间」和「本来就要开新页但碰巧 id 相同」这类混淆——尺寸差异让「空间确实被释放并复用」成为唯一解释。（补充背景：图集页从 1024×1024 起步，700×700 的大 tile 能有效施压分配器。）

**练习 3**：这两条 GPU 回归测试能否改成 mock 掉 `wgpu::Device` 来在无 GPU 环境运行？

**答案**：不能，`wgpu::Device` 是具体类型而非 trait，没有 mock 点；而且这两条测试要验证的恰恰包含 `queue.write_texture` 路径上的真实行为。库作者的选择是把「纯逻辑」抽成可独立测试的函数（如 `swizzle_upload_data`）用 CPU 测试覆盖，把「必须碰 GPU」的留在集成层——这是对「测试金字塔」的务实妥协。

### 4.3 cosmic_text_system 的测试：纯 CPU 的排版测试

#### 4.3.1 概念说明

`CosmicTextSystem` 是本 crate 测试最密集的模块（21 条），因为它有一ideal 特性：**文本整形（shaping）与字体回退完全是 CPU 计算**，一个 GPU 都不需要。更妙的是它的测试设施做到了「密封」（hermetic）——不依赖系统字体，而是把 IBM Plex Sans 的 ttf 字节用 `include_bytes!` 嵌进测试，配 `new_without_system_fonts` 构造。这样测试结果在任何机器上都确定可复现。

这 21 条测试清晰分成三层，是教科书级的测试金字塔：

1. **纯函数层（12 条）**：`pick_covering_slot`、`compute_run_spans`、`clip_font_runs`、`is_paragraph_separator`——连字体都不加载，用假数据直接测函数；
2. **密封排版层（8 条）**：用内嵌字体走 `layout_line` 完整链路；
3. **集成层（1 条）**：把 `CosmicTextSystem` 包进 gpui 的 `TextSystem`/`WindowTextSystem`，从 gpui 公开 API 入口复现原始崩溃。

#### 4.3.2 核心流程

先认识测试搭手架的方式，再看两个家族测什么。

**搭手架**（测试模块顶部）：`text_system()` 构造密封实例 → `layout_text()` 封装「单字体 run + 14px + layout_line」的调用惯例：

```text
text_system()
  ├─ CosmicTextSystem::new_without_system_fonts("IBM Plex Sans")   # 空 fontdb，不碰系统
  └─ add_fonts(vec![Cow::Borrowed(IBM_PLEX)])                      # 只装内嵌字体
layout_text(&system, text)
  └─ system.layout_line(text, px(14.0), &[FontRun { len: text.len(), font_id }])
```

**家族 A：段落分隔符与双向文本**。被测函数 `is_paragraph_separator` 判定字符的 `Bidi_Class` 是否为 `B`（[src/cosmic_text_system.rs:716-718](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L716-L718)），`contains_paragraph_separator` 先做 ASCII 字节快查再逐字符判定（[src/cosmic_text_system.rs:720-729](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L720-L729)）。含分隔符的行会被 `layout_line` 切成多个双向段落分别整形再拼接——测试就要保证切分、拼接、索引在所有边界情形下正确。

**家族 B：字体回退链划分**。`compute_run_spans` 把一段文本按「哪个字体槽覆盖哪个字符」切成若干 `RunSpan`，`pick_covering_slot` 决定单个字符归属哪个槽。测试通过注入一个假的 `covers: Fn(FontId, char) -> bool` 闭包来伪造字体覆盖表——不需要任何真实字体。

#### 4.3.3 源码精读

**家族 A：段落分隔符（8 条）**

- [src/cosmic_text_system.rs:1022-1026](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1022-L1026) — 测试常量 `SEPARATORS`：枚举全部 7 个 `Bidi_Class=B` 码点（`\n`、`\r`、`U+001C`、`U+001D`、`U+001E`、`U+0085`、`U+2029`）。后续测试对它们做笛卡尔积式遍历，保证「换行符碰巧被测过、冷门分隔符漏测」这种事不会发生。

- [src/cosmic_text_system.rs:1045-1064](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1045-L1064) — 唯一的集成层测试 `shape_text_with_mixed_direction_paragraphs`。注释点明出身：*"Mirrors the original crash: mixed-direction text reaching the shaper through `shape_text`, which only splits lines on `\n`"*。它把平台文本系统包进 gpui 的 `TextSystem` + `WindowTextSystem`，对 `"first line\nא\u{001C}A"` 调 `shape_text`，断言得到 2 行、第二行宽度非零——完整复现用户视角的崩溃路径。

- [src/cosmic_text_system.rs:1066-1087](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1066-L1087) — `layout_line_with_mixed_direction_paragraphs`：7 个分隔符 × 2 种语序（RTL 在前/在后）共 14 种组合，逐一断言 `layout.len == text.len()`、宽度大于零、至少一个 run 有字形。防止分段整形丢字或产出空排版。

- [src/cosmic_text_system.rs:1089-1106](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1089-L1106) — `layout_line_with_separators_at_line_edges`：分隔符在行首、行尾、连续出现等退化位置——边界条件专项。

- [src/cosmic_text_system.rs:1111-1133](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1111-L1133) — 本模块最有代表性的一条：`layout_line_keeps_indices_and_positions_ordered_across_paragraphs`。对 `"ab\u{001C}cd\u{2029}ef"` 断言三件事：每个字形的 `index` 是**绝对字节偏移**且落在字符边界上；相邻字形 `index` 严格递增、`position.x` 单调不减；整行宽度大于单独排 `"ab"` 的宽度。注释解释了为什么——否则「光标定位与点击测试会错位」。这就是把「不变量」写成断言的典范。

- [src/cosmic_text_system.rs:1137-1159](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1137-L1159) — `layout_line_with_font_run_straddling_a_separator`：字体 run 的边界落在 RTL 段落中间（不在段落边界上），验证 `clip_font_runs` 会把 run 正确裁剪到各段。

- [src/cosmic_text_system.rs:1163-1179](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1163-L1179) — `layout_line_without_separators_takes_fast_path`：纯 ASCII、纯 RTL、段内混合三种**不含分隔符**的文本，先断言 `!contains_paragraph_separator(text)` 走的确实是快路径，再断言排版正确——保证「加快路径」没有改变原有行为。

- [src/cosmic_text_system.rs:1181-1197](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1181-L1197) — `paragraph_separator_detection`：对判定函数本身做正反两面测试（7 个分隔符全正例；空串、ASCII、希伯来字母、制表符、emoji 全反例——注意 tab 和 emoji 都**不是**段落分隔符）。

**家族 B：回退链与纯函数（12 条）**

- [src/cosmic_text_system.rs:1236-1295](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1236-L1295) — `pick_covering_slot` 的 6 条测试：主字体覆盖则优先主字体（`None` 槽）、回退链按声明顺序穿透（只有 2 号字体覆盖 `字` 时返回 `Some(1)`）、全都不覆盖时返回 `None` 交给 cosmic-text 内建脚本回退、空链返回主字体、`slot_font_id` 的槽位→FontId 解析。

- [src/cosmic_text_system.rs:1297-1409](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1297-L1409) — `compute_run_spans` 的 7 条测试，覆盖：无链时单一主字体 span；多字节字符按**字节偏移**切分（`"a字b"` 在 1 和 4 处切，因为 `字` 占 3 字节）；外层 run 带偏移（`run_offset=2`）；组合附加符号必须与基底字符同槽；ZWJ（零宽连接符）不许拆散 emoji 序列 `\u{1F469}\u{200D}\u{1F467}`；相邻同槽字符合并成一个 span；空 run 返回空。每一条都对应一类真实文本（天城文、emoji 家庭组合、中日韩）。

```rust
// "a字b"：primary 只覆盖 ASCII，fallback 只覆盖非 ASCII
assert_eq!(
    spans.as_slice(),
    &[
        span(0, 1, None, primary),      // 'a'
        span(1, 4, Some(0), fid(1)),    // '字' 占 3 字节 → [1, 4)
        span(4, 5, None, primary),      // 'b'
    ]
);
```

- [src/cosmic_text_system.rs:1199-1234](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1199-L1234) — `font_runs_are_clipped_to_segment`：纯数据进出，验证 `clip_font_runs` 对区间取裁剪的三种情形与空区间。

#### 4.3.4 代码实践

**实践目标**：通过运行测试 + 手工复算断言，确认「排版测试完全不需要 GPU」，并学会用断言反推函数行为。

**操作步骤**：

1. 分两批运行（都不需要 GPU）：
   ```bash
   cargo test -p gpui_wgpu cosmic_text_system
   cargo test -p gpui_wgpu run_spans
   ```
2. 打开 [src/cosmic_text_system.rs:1307-1330](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1307-L1330)，对 `"a字b"` 手工复算：`'a'` 是 1 字节、`'字'` 是 3 字节、`'b'` 是 1 字节，所以字节偏移序列是 0、1、4、5；对照断言中三个 span 的 `start/end`。
3. 阅读一条你不熟悉的测试（例如 ZWJ 那条），先只看 `text` 与断言，猜猜 `compute_run_spans` 为什么必须这么切，再看 `covers` 闭包的定义验证猜测。

**需要观察的现象**：第 1 步在无 GPU 的机器上同样全绿（这些测试只碰 CPU 与内嵌字体字节）；第 2 步手算的 0/1/4/5 与断言中的 span 边界完全吻合。

**预期结果**：21 条 cosmic_text 测试全部通过（待本地验证）；手算偏移与断言一致（这条可以直接从字符编码规则推出，UTF-8 中 ASCII 1 字节、`字` 3 字节、`א` 2 字节）。

#### 4.3.5 小练习与答案

**练习 1**：`text_system()` 为什么用 `new_without_system_fonts` 而不是正常的构造函数？这对测试的「密封性」意味着什么？

**答案**：正常构造会扫描并加载系统字体，导致测试行为随机器不同而变化（不同机器装的不同字体、字重不同，排版结果和回退行为都不可控）。`new_without_system_fonts`（[src/cosmic_text_system.rs:78](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L78)）从空 fontdb 起步，再 `add_fonts` 只装入 `include_bytes!` 嵌入的 IBM Plex——测试在任何机器上跑出的字形、宽度都相同，失败即真失败。这也是 u5-l3 将展开的字体加载机制的反向应用。

**练习 2**：`no_coverage_returns_primary` 测试里，当回退链中没有任何字体覆盖字符时，`pick_covering_slot` 返回 `None`（主字体槽）而不是报错。为什么这样设计是安全的？

**答案**：返回 `None` 意味着这个字符仍交给主字体（cosmic-text 底层还有内建的按文字系统（script）选字体的回退机制会再兜底一次，测试注释也点明了这一点）。对排版系统而言「尽力显示一个替代字形」远好于「报错中断整行渲染」——缺字形最多显示豆腐块，抛错会让整个编辑器无法显示这行文本。

**练习 3**：家族 A 里为什么要有 `layout_line_keeps_indices_and_positions_ordered_across_paragraphs` 这条「不变量」测试，而不只是逐例断言宽度大于零？

**答案**：宽度断言只能证明「排出来了」，不能证明「排对了」。分段整形再拼接的实现里，最隐蔽的 bug 是各段使用段内相对索引、拼接后字形 `index` 不再是全行绝对偏移——排版宽度看起来正常，但光标定位和鼠标点击测试（hit testing）会整体错位。把「索引绝对、严格递增、位置单调」写成跨段不变量，一次断言守住所有分段组合，比堆更多用例更有效。

### 4.4 benches/layout_line.rs：criterion 基准

#### 4.4.1 概念说明

`layout_line` 是 Zed 里最热的函数之一：编辑器每一行文本的显示宽度、字形位置都由它计算，输入每敲一个字符就可能触发。它值得一个专门的 criterion 基准目标来守护性能。

基准文件本身就是一份优秀的「语料设计」示范，三个决策值得细看：

1. **语料是真实的源代码**：`code_text()` 直接把本 crate `compute_run_spans` 函数的源码文本（约 470 字符）重复 8 遍，拼成约 3800 字符的「单行代码文本」——正是 Zed 用户长行的典型形态（连续 ASCII、含大量空白与标点）。
2. **换行被替换成空格**：`.replace('\n', " ")` 不是随手为之，文件注释专门解释了原因（下面精读）。
3. **第三条基准主动走慢路径**：`mixed_direction_paragraphs` 在语料末尾追加 `U+001C` 和两个希伯来字母，强制触发分段整形路径，与快路径形成对照测量。

先建立配置层面的认知：[Cargo.toml:51-57](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L51-L57) 声明了 dev-dependencies（`criterion`、`naga`）与 `[[bench]] name = "layout_line" harness = false`——后者是使用 criterion 的固定搭配（不用标准 harness，由 `criterion_main!` 生成入口，见 [benches/layout_line.rs:103-104](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/benches/layout_line.rs#L103-L104)）。

#### 4.4.2 核心流程

```text
准备阶段:
  CosmicTextSystem::new_without_system_fonts("Lilex")     # 密封构造，与 4.3 同款
    .add_fonts([Lilex-Regular.ttf, IBMPlexSans-Regular.ttf])  # 内嵌字体
  font_id_no_fallback     = font("Lilex")                     # 无回退链
  font_id_with_fallback   = font("Lilex") + fallbacks=["IBM Plex Sans"]
  text                = code_text()        # ~3800 字符 ASCII，无 \n（有断言守护）
  text_mixed_direction = text + "\u{001C}\u{05D0}\u{05D1}"    # 分隔符 + 2 个 RTL 字符

测量阶段（criterion 对每个场景统计采样）:
  no_fallback              → layout_line(text, 14px, 单 run 无回退)        # 快路径基线
  with_fallback_ascii      → layout_line(text, 14px, 单 run 带回退链)      # 回退链开销（ASCII 永不触发切换）
  mixed_direction_paragraphs → layout_line(text_mixed, 14px, 单 run)       # 分段整形慢路径
```

三条基准构成一个微型「性能矩阵」：基线 → 加一项成本 → 再加一项成本，两两相减就能读出每项机制的代价。

#### 4.4.3 源码精读

- [benches/layout_line.rs:6-8](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/benches/layout_line.rs#L6-L8) — 两份内嵌字体：Lilex（等宽，主字体）与 IBM Plex Sans（回退字体），从仓库 `assets/fonts/` 用 `include_bytes!` 取字节。与 4.3 的密封策略完全同构。

- [benches/layout_line.rs:10-47](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/benches/layout_line.rs#L10-L47) — `code_text()`。文件级注释直接回答了本讲实践任务的第一问：

```rust
// `layout_line` is handed one line at a time, already split on `\n` by its
// callers, so the newlines are replaced rather than kept: leaving them in would
// make this measure the multi-paragraph path instead of the common one, since
// `\n` is itself a bidi paragraph separator.
```

翻译过来：真实调用方（gpui 的 `shape_text`）会先按 `\n` 把文本切成一行一行再交给 `layout_line`，所以基准语料必须模拟「单行」形态；而 `\n` 恰好是双向段落分隔符（4.3 家族 A 的 `SEPARATORS` 之首），如果保留换行符，测到的就是「多段落分段整形」的慢路径，而不是最常见的快路径——基准就失真了。第 45-46 行 `.repeat(8).replace('\n', " ")` 是落地：先重复 8 份凑长度，再统一替换成空格保持行长。

- [benches/layout_line.rs:63-71](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/benches/layout_line.rs#L63-L71) — 慢路径语料的构造与守护断言：

```rust
let text_mixed_direction = text.clone() + "\u{001c}\u{05d0}\u{05d1}";
assert!(
    !text.contains('\n'),
    "fast-path corpus must contain no separator"
);
```

`U+001C`（Information Separator Three，4.3 家族 A 的常客）开启一个新段落，后面两个希伯来字母 `א ב`（RTL）让该段落方向与前面 3800 字符的 LTR 不同——整个 `layout_line` 被迫切换到「按段落切分 → 逐段整形 → 拼接」的慢路径。`assert!` 则保证快路径语料的纯洁性：谁要是改了 `code_text()` 让 `\n` 混进来，基准立刻在启动时报错而不是默默测错对象。

- [benches/layout_line.rs:86-100](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/benches/layout_line.rs#L86-L100) — 三个 `bench_function`：`no_fallback`（基线）、`with_fallback_ascii`（同一 ASCII 文本但字体带 IBM Plex 回退链——因为 ASCII 全被主字体 Lilex 覆盖，回退链永远不触发切换，测的是**每字符检查覆盖的固定开销**）、`mixed_direction_paragraphs`（慢路径）。每次迭代都是完整的 `system.layout_line(&text, px(14.0), &runs)` 调用。

#### 4.4.4 代码实践

**实践目标**：运行 `layout_line` 基准，读取三组耗时，并回答两个语料设计问题（对应总实践任务的第二部分）。

**操作步骤**：

1. 在仓库根目录运行（指定 bench 目标名；由于本 crate 只有这一个 bench 目标，`cargo bench -p gpui_wgpu` 等价）：
   ```bash
   cargo bench -p gpui_wgpu layout_line
   ```
   首次运行需要编译 gpui、wgpu 等依赖，耗时可能以十分钟计，属正常。
2. 若只想跑其中一个场景，用 criterion 的过滤语法（`--` 之后是组名/函数名过滤）：
   ```bash
   cargo bench -p gpui_wgpu -- no_fallback
   ```
3. 记录三个场景的 `time: [… … …]`（criterion 输出的均值/区间），计算 `with_fallback_ascii ÷ no_fallback` 与 `mixed_direction_paragraphs ÷ no_fallback` 两个比值。
4. 可选：用浏览器打开 `target/criterion/layout_line/report/index.html` 查看可视化对比。

**需要观察的现象**：终端按 `layout_line/no_fallback`、`layout_line/with_fallback_ascii`、`layout_line/mixed_direction_paragraphs` 三组输出统计；混合方向场景的每次迭代耗时显著高于另两个（它要额外做段落切分与多次整形调用）；`target/criterion/` 目录下生成基准报告。

**预期结果**：三组均能完成测量；两个比值的具体数值**待本地验证**（定性预期：`mixed_direction_paragraphs` 最慢，`with_fallback_ascii` 略慢于 `no_fallback`，因为整行 ASCII 未发生字体切换，仅多付每字符覆盖检查的成本）。

#### 4.4.5 小练习与答案

**练习 1**（即实践任务问题一）：`code_text()` 为什么要把换行替换成空格，而不是直接删掉或保留？

**答案**：调用方（`shape_text`）总是先按 `\n` 切行再调 `layout_line`，所以基准必须喂「单行」文本才有代表性。保留 `\n` 会让 `\n` 这个双向段落分隔符把语料切成几千个段落，测成分段整形慢路径（与第三条基准混淆）；直接删掉则会改变行长与空白分布、微调整形压力；替换成空格既维持了「单行、无分隔符」的语义，又保住了原始行长——最贴近真实代码行的形态。

**练习 2**（即实践任务问题二）：`text_mixed_direction` 相比 `text` 额外测了什么？

**答案**：额外测了**含段落分隔符的双向文本路径**。语料末尾追加 `U+001C`（开启新段落）和两个希伯来字母（RTL 方向），迫使 `layout_line` 走「检测分隔符 → 切分多段落 → 逐段（含 RTL 段）整形 → 按字节偏移拼接」的慢路径。与 `no_fallback` 的快路径对照，二者之差就是分段整形机制的净开销——正是 4.3 家族 A 那些正确性测试所守护的同一条代码路径的性能面。

**练习 3**：为什么 `with_fallback_ascii` 场景要特意选择「全部 ASCII、主字体全覆盖」的语料？如果想测「回退真正发生」的开销，语料该怎么改？

**答案**：这个场景隔离测量**回退链的固定检查开销**：每个字符都要问一遍「主字体覆盖吗？覆盖则留在主槽」，但因为全被覆盖，从不发生真正的字体切换与 span 切分，测到的是纯检查成本。若要测真实切换开销，应混入主字体不覆盖的字符（例如把 `code_text` 中部分 ASCII 换成汉字或希伯来字母），让 `compute_run_spans` 真正切出多个 span、整形器在两个字体间往返——但那就与混合方向场景部分耦合了，需要构造新的对照组。

## 5. 综合实践

**任务：产出一份《gpui_wgpu 测试与基准体检报告》**，把本讲四类验证设施串成一次完整的动手闭环。建议输出为一份 markdown 笔记（放在你自己的目录，不要放进仓库）。

1. **测试分类与实测**：运行 `cargo test -p gpui_wgpu`，把结果整理成第 3 节那张分类表的「实测版」——每个模块一行，列出通过数/失败数；失败的逐条标注「环境限制（无 GPU 适配器）」或「疑似回归」。无 GPU 环境下图集两条测试的失败属预期，标注「待确认」即可。
2. **失败溯源练习**：若有失败，顺着失败信息找到 [src/wgpu_atlas.rs:417-424](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L417-L424) 的 `request_adapter` 调用，在报告里写清楚「失败发生在测试脚手架的哪一行、为什么与被测逻辑无关」。
3. **基准观测**：运行 `cargo bench -p gpui_wgpu layout_line`，记录三组耗时与两个比值，用两三句话解释比值背后的机制（对照 4.4.5 的答案自查）。
4. **（选做，本地副本实验）验证守门人**：在本地克隆里对 `src/shaders_storage.wgsl` 做一处破坏性小改动，运行 `cargo test -p gpui_wgpu shader_is_valid` 确认 naga 校验测试能捕获并给出行列号，随后 `git checkout` 还原，并把这段「破坏 → 捕获 → 还原」的输出贴进报告。

完成这份报告后，你对本 crate 的「验证面」就有了亲手实测过的地图——后续每一讲做源码实验时，都知道改完该跑哪些命令来确认没弄坏东西。

## 6. 本讲小结

- 本 crate 共 30 个单元测试 + 1 个 criterion 基准：**28 个纯 CPU、2 个需要真实 GPU 适配器**（都在 `wgpu_atlas.rs`，无 GPU 环境失败属环境限制）。
- WGSL 着色器没有编译期检查，本 crate 用 dev-dependency 形式的 naga 在测试里对三个拼接变体做 CPU 侧解析与校验，能力集（空 vs `DUAL_SOURCE_BLENDING`）本身成为变体差异的声明。
- `webgl_record_sizes_match_shader_word_strides` 用 `size_of` 断言锁死「Rust 结构体字节数 = WGSL word 数 × 4」的跨语言契约，把最隐蔽的静默渲染错乱变成测试失败。
- 图集的两条 GPU 回归测试分别守护「移除后跳过滞留上传」与「remove 真正归还分配空间」；`cosmic_text_system` 的 21 条纯 CPU 测试呈纯函数 → 密封排版 → gpui 集成的三层金字塔，密封性靠内嵌字体 + `new_without_system_fonts`。
- `layout_line` 基准的语料设计三要点：真实源码作长行语料、换行替换成空格以保持快路径代表性（`\n` 本身是双向段落分隔符）、第三条场景用 `U+001C` + RTL 字符主动测慢路径。
- 读测试是读源码的高效入口：回归测试的注释直接指出历史 bug 的位置，不变量型测试（字形索引绝对且递增）揭示了光标定位等下游依赖的隐含契约。

## 7. 下一步学习建议

本讲是第一单元（初识 gpui_wgpu）的收尾：你已经知道它是什么（u1-l1）、如何组织（u1-l2）、如何验证（本讲）。下一讲进入第二单元：**u2-l1「WgpuContext：instance、adapter、device 与 queue 的创建」**，正式开始精读功能源码。你会发现本讲埋下的几处伏笔都会被接上：

- `wgpu_atlas.rs` 测试脚手架里手写的 Instance/Adapter/Device 创建流程，正是 `WgpuContext::create_device` 的简化版；
- `downlevel_defaults()` 这个保守选择，会再次出现在 WebGL2 路径的 `downlevel_webgl2_defaults` 中；
- 顺带可以先读一眼 [src/wgpu_context.rs:585-606](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L585-L606) 的 `test_parse_device_id`——它是你读 u2-l2 适配器选择算法（含 `ZED_DEVICE_ID` 覆盖）前最轻松的热身。

如果想先巩固本讲内容，建议重读 [benches/layout_line.rs:10-47](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/benches/layout_line.rs#L10-L47) 的注释与 [src/cosmic_text_system.rs:1022-1026](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/cosmic_text_system.rs#L1022-L1026) 的 `SEPARATORS` 常量——「`\n` 是段落分隔符」这条事实在 u5-l4（layout_line 排版路径）还会第三次出现。
