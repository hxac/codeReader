# Settings:分层配置系统

## 1. 本讲目标

学完本讲,你应该能够:

1. 看懂 Zed 设置系统的三层分工:`settings_content`(JSON 面向的内容定义)、`settings_macros`(注册与合并的宏)、`settings`(运行时的 `SettingsStore`)。
2. 用 `#[derive(RegisterSetting)]` + `Settings` trait 定义一个带默认值的新设置项,并让它出现在 `default.json` 与 JSON schema 中。
3. 说出设置值从默认值到项目级 `.zed/settings.json` 的分层覆盖与合并顺序,以及"按文件路径取值"的规则。
4. 描述修改 `settings.json` 保存后,设置如何无需重启就生效(文件监听 → 解析迁移 → 重算 → 全窗口重绘)。
5. 结合 `git_gutter_width` 从 `Option<f32>` 演进为 `GitGutterWidth` 枚举的真实案例,解释 migrator 中的设置迁移如何把旧用户配置改写成新格式。

## 2. 前置知识

- **Global 与 Entity**:GPUI 把全局状态放在 `App` 上(`impl Global for T`),任何代码都能通过 `cx.global::<T>()` 读取。`SettingsStore` 就是一个 Global(依赖 u2-l2 的知识)。
- **Option 作为"合并单元"**:Zed 的设置内容结构里几乎所有字段都是 `Option<T>`。`None` 不是"假",而是"这一层没说,听上层的";`Some(v)` 才是"这一层明确覆盖"。这是整个分层系统的基础。
- **serde 与 snake_case**:设置 JSON 里的键名与 Rust 字段名通过 serde 对应;枚举用 `#[serde(rename_all = "snake_case")]` 后,`Default` 变体写成 `"default"`,带数据的变体 `Custom(x)` 写成 `{"custom": x}`。
- **inventory**:一个"编译期自注册"库——宏在代码里埋下 `inventory::submit! {...}`,运行时用 `inventory::iter::<T>()` 就能枚举出所有埋点,不需要手工维护一张注册表。
- **tree-sitter**:Zed 用它解析 JSON 文本来做保留注释的迁移编辑,这里只需要知道"它能按语法结构定位并替换文本片段"。

一个直觉模型:设置系统像一座**叠了五层玻璃片**的投影仪——默认值是最底层的玻璃片,用户设置、服务器设置、项目设置依次叠上去;每一层只画自己想改的部分(`Some`),没画的部分(`None`)透过去看下层。最终屏幕上的图像,就是从上往下第一层有画的内容。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/settings/src/settings.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings.rs) | settings crate 的门面:模块声明、`RegisterSetting` 再导出、`init()`、内嵌 `assets/settings/default.json` 的 `SettingsAssets` |
| [crates/settings_macros/src/settings_macros.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_macros/src/settings_macros.rs) | 三个过程宏:`MergeFrom`(字段级合并)、`RegisterSetting`(inventory 自注册)、`with_fallible_options`(单个字段解析失败不炸整个文件) |
| [crates/settings/src/settings_store.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs) | 核心:`Settings` trait、`SettingsStore` 的分层存储与 `recompute_values` 合并、解析+迁移入口 |
| [crates/settings/src/settings_file.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_file.rs) | `watch_config_file` / `watch_config_dir`:设置文件的监听与重读 |
| [crates/settings/src/content_into_gpui.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/content_into_gpui.rs) | `IntoGpui` trait:把内容层的 `FontSize`/`PixelSetting` 等换成 gpui 的 `Pixels` 等类型 |
| [crates/settings_content/src/editor.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/editor.rs) | 内容层定义:`GitGutterWidth` 枚举、`GutterContent` 等编辑器设置的 JSON 形态 |
| [crates/editor/src/editor_settings.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_settings.rs) | 消费层:`EditorSettings` 如何从 `SettingsContent` 提炼出强类型 `Gutter` |
| [crates/migrator/src/migrator.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs) | 迁移引擎:TreeSitter / Json 两类迁移,按时间顺序链接执行 |
| [crates/migrator/src/migrations.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrations.rs) | 按日期组织的迁移模块清单,以及 `migrate_settings` 辅助函数(遍历 root/渠道/平台/profile 作用域) |
| [crates/migrator/src/migrations/m_2026_08_17/settings.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrations/m_2026_08_17/settings.rs) | 本讲案例:`git_gutter_width` 数值 → `{"custom": n}` 的迁移 |
| [assets/settings/default.json](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/assets/settings/default.json) | 全部内置默认值,既是运行时兜底,也是面向用户的文档 |

另外两个配角:`crates/settings/src/editorconfig_store.rs`(项目内 `.editorconfig` 的解析与事件)和 [crates/vim_mode_setting/src/vim_mode_setting.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/vim_mode_setting/src/vim_mode_setting.rs)(最小设置项的绝佳范例)。

## 4. 核心概念与源码讲解

### 4.1 设置项的定义与注册

#### 4.1.1 概念说明

Zed 的设置定义分成"内容层"和"消费层"两半:

- **内容层**(`settings_content` crate):定义 JSON 长什么样。每个设置组是一个字段全为 `Option<T>` 的结构体(如 `GutterContent`),根结构 `SettingsContent` 汇总所有组。这一层不依赖 gpui 的重类型,数值用 `FontSize`、`PixelSetting` 这类轻量包装。
- **消费层**(各业务 crate):定义"我关心哪一块"。比如 editor crate 定义 `EditorSettings`,实现 `Settings` trait 的 `from_settings(&SettingsContent) -> Self`,从合并后的内容里提炼出强类型、无 `Option` 的最终值,供渲染和逻辑代码直接使用。

连接两层的是**注册机制**:老资料里常说的 `settings!` 宏,在当前代码里已经统一替换为 `#[derive(RegisterSetting)]`——这个 derive 会展开成一次 `inventory::submit!`,把"如何构造这个设置类型的空壳"和"如何从 `SettingsContent` 计算它的值"两件事登记进全局清单;`SettingsStore` 创建时枚举清单,逐个实例化。这样新增设置项不需要改任何中心化注册表。

`from_settings` 里大量使用 `unwrap()` 是**故意的**:默认值必须来自 `default.json`——缺了默认值就在启动时 panic,逼迫开发者把默认值写进 `default.json`(它同时是面向用户的文档和 JSON schema 的来源)。

#### 4.1.2 核心流程

定义一个新设置项的完整链路:

```text
1. settings_content:在组合适的结构体里加 Option<T> 字段(根级则加在 SettingsContent)
2. assets/settings/default.json:补上默认值(必填,否则启动 panic)
3. 消费 crate:#[derive(RegisterSetting)] struct MySetting(...);
            impl Settings { fn from_settings(content) -> Self { ...unwrap()... } }
4. 编译期:RegisterSetting 展开 inventory::submit!,类型进入全局清单
5. 运行期:SettingsStore::new → load_settings_types → inventory::iter
          → 对每个已注册类型调用 from_settings(merged_settings) 存入全局值
6. 业务代码:MySetting::get_global(cx) / MySetting::get(Some(location), cx) 读取
```

注册之后,该字段还会因为 `schemars::JsonSchema` 自动进入 JSON schema——用户编辑 `settings.json` 时得到的补全与校验就是从这来的。

#### 4.1.3 源码精读

先看最小范例——`VimModeSetting`,一个只有 8 行的完整设置项:

```rust
#[derive(RegisterSetting)]
pub struct VimModeSetting(pub bool);

impl Settings for VimModeSetting {
    fn from_settings(content: &SettingsContent) -> Self {
        Self(content.vim_mode.unwrap())
    }
}
```

这段代码定义了一个布尔设置:内容层字段是 [settings_content.rs:L302-L305](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/settings_content.rs#L302-L305) 中的 `pub vim_mode: Option<bool>`,`unwrap()` 的底气来自 `default.json` 里必有默认值。

`RegisterSetting` 到底做了什么?看宏的实现:

```rust
#[proc_macro_derive(RegisterSetting)]
pub fn derive_register_setting(input: TokenStream) -> TokenStream {
    let input = syn::parse_macro_input!(input as DeriveInput);
    let type_name = &input.ident;

    quote! {
        settings::private::inventory::submit! {
            settings::private::RegisteredSetting {
                settings_value: || { /* 构造该类型的空值容器 */ },
                from_settings: |content| Box::new(<#type_name as settings::Settings>::from_settings(content)),
                id: || std::any::TypeId::of::<#type_name>(),
            }
        }
    }
    .into()
}
```

见 [settings_macros.rs:L85-L105](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_macros/src/settings_macros.rs#L85-L105):它把类型名、`from_settings` 函数指针和 `TypeId` 打包成一个 `RegisteredSetting` 提交给 inventory。`settings` crate 则在 [settings.rs:L21-L25](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings.rs#L21-L25) 开了一个 `private` 模块,只暴露给宏生成的代码用。运行时的收集点在 [settings_store.rs:L420-L424](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L420-L424):

```rust
fn load_settings_types(&mut self) {
    for registered_setting in inventory::iter::<RegisteredSetting>() {
        self.register_setting_internal(registered_setting);
    }
}
```

`Settings` trait 本体在 [settings_store.rs:L60-L129](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L60-L129),其中 `from_settings` 的文档注释明确写着"缺默认值就应该 panic"([L70-L74](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L70-L74));`get_global`/`get`/`override_global` 等读取 API 也都在这个 trait 上,业务代码经由它们访问 store。

接下来是本讲的主角案例。`git_gutter_width` 控制 gutter 里 git diff 指示条的宽度,它的内容层定义在 [editor.rs:L484-L505](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/editor.rs#L484-L505):

```rust
#[serde(rename_all = "snake_case")]
pub enum GitGutterWidth {
    /// Width scales automatically with the buffer font size.
    #[default]
    Default,
    /// A fixed pixel width for the git diff indicators.
    Custom(crate::PixelSetting),
}
```

在 2026 年 8 月之前,这个设置是 `Option<f32>`(数字或留空);现在它是二选一枚举:`"default"` 表示随字号自动缩放,`{"custom": 6.0}` 表示固定像素。容纳它的 `GutterContent` 见 [editor.rs:L507-L540](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/editor.rs#L507-L540),字段 `pub git_gutter_width: Option<GitGutterWidth>`([L539](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/editor.rs#L539)),结构体上标了 `#[with_fallible_options]`——这个属性宏([settings_macros.rs:L109-L152](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_macros/src/settings_macros.rs#L109-L152))给每个 `Option` 字段加上容错反序列化:某个字段写错了类型只会让这一个字段变 `None`,不会让整个 settings 文件解析失败。

默认值登记在 [default.json:L693-L709](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/assets/settings/default.json#L693-L709):

```json
"gutter": {
    // ...
    // The width of the git diff hunk indicators in the gutter.
    // Use "default" to scale with font size, or {"custom": <pixels>} for a fixed width.
    "git_gutter_width": "default",
},
```

这份文件通过 `RustEmbed` 编译期内嵌进二进制([settings.rs:L120-L125](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings.rs#L120-L125)),`init()` 在应用启动时用它构造 `SettingsStore` 并设为 Global([settings.rs:L127-L131](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings.rs#L127-L131))。

消费层把它变成无 `Option` 的强类型:[editor_settings.rs:L144-L153](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_settings.rs#L144-L153) 定义 `pub struct Gutter { ..., pub git_gutter_width: settings::GitGutterWidth }`,`from_settings` 里 `git_gutter_width: gutter.git_gutter_width.unwrap()`([L271](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_settings.rs#L271))完成"内容层 Option → 消费层确定值"的收口。最终在渲染管线里被 `match` 消费——[element.rs:L5322-L5325](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5322-L5325):

```rust
fn gutter_strip_width(line_height: Pixels, cx: &App) -> Pixels {
    match EditorSettings::get_global(cx).gutter.git_gutter_width {
        GitGutterWidth::Custom(width) => px(*width),
        GitGutterWidth::Default => (0.275 * line_height).floor(),
    }
}
```

`Custom` 给固定像素,`Default` 按行高(即字号)的 0.275 倍缩放——这正是"默认值不是常量"这个重构动机的体现:旧的 `Option<f32>` 无法表达"随字号缩放"这种语义。

顺带一提内容层与 gpui 类型的桥:`PixelSetting` 是内容层包装的 `f32`([settings_content.rs:L59-L68](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/settings_content.rs#L59-L68)),`IntoGpui` trait([content_into_gpui.rs:L12-L15](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/content_into_gpui.rs#L12-L15))提供 `FontSize → Pixels`、`PixelSetting → Pixels` 等转换([L79-L85](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/content_into_gpui.rs#L79-L85)),让 `settings_content` 不必依赖 gpui;`element.rs` 这里则直接 `px(*width)` 完成同样的换算。

#### 4.1.4 代码实践

**实践:在测试里驱动一个设置项的变化(源码阅读 + 本地小实验)**

1. **实践目标**:验证"改 JSON → 设置值变化"的最小闭环,不涉及 UI。
2. **操作步骤**:
   - 阅读 [settings_store.rs:L527-L543](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L527-L543) 的 `update_user_settings`:它是测试专用辅助函数,把修改闭包应用到用户设置内容、序列化回 JSON 文本,再走一遍正式的 `set_user_settings` 管线。
   - 在 settings crate 已有测试(如 `settings_store.rs` 底部的 `#[cfg(test)]` 模块)里模仿写一个:构造 `SettingsStore::test(cx)`,用 `update_user_settings` 把某个布尔设置改为 `true`,再断言 `Settings::get_global(cx)` 读到的值变化。
   - 运行:`cargo test -p settings`(或 `cargo test -p settings settings_store`)。
3. **需要观察的现象**:测试中修改立即反映在 `get_global` 的返回值,无需任何"刷新"调用——因为 `set_user_settings` 内部会触发 `recompute_values`。
4. **预期结果**:断言通过。具体测试代码需要按你本地该文件当时的测试组织方式调整,**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `GutterContent` 的字段都是 `Option<T>`,而消费层的 `Gutter` 不是?

答案:`Option` 是分层合并的载体——`None` 表示"该层未设置,继承下层(默认值/用户设置/项目设置)",`Some` 表示覆盖。合并完成后值一定存在(默认值兜底),所以消费层可以也应该是无 `Option` 的确定类型,用起来不需要再判空。

**练习 2**:`"git_gutter_width": {"custom": 6.0}` 是怎么和 `GitGutterWidth::Custom(PixelSetting(6.0))` 对上的?

答案:枚举标了 `#[serde(rename_all = "snake_case")]`,serde 对带数据的变体采用 externally tagged 的默认表示——变体名作为键(`"custom"`),变体数据作为值;`Default` 无数据变体则序列化为字符串 `"default"`。

**练习 3**:忘记在 `default.json` 里给新设置加默认值会发生什么?

答案:`from_settings` 里的 `unwrap()` 会在设置加载时 panic。这是设计意图:`Settings::from_settings` 的文档注明默认值缺失就该 panic,迫使开发者补全 `default.json`(它同时是文档与 schema 来源)。

### 4.2 SettingsStore:分层合并与按路径读取

#### 4.2.1 概念说明

`SettingsStore` 是一个 GPUI Global,是所有设置的唯一权威来源。它内部维护**多份内容**而非一份:默认值、用户设置、管理员全局设置、服务器设置、扩展设置、以及按"(worktree, 目录)"索引的一组项目本地设置。任何一份变化,都会**全量重算**出:

- 一份全局合并结果 `merged_settings`;
- 每个"含有本地设置的目录"一份**该目录专属**的合并快照(局部值)。

读取时,`get(Some(location))` 会找**路径包含关系上最深**的那份局部快照;`get(None)`/`get_global` 拿全局值。这就是"项目里的 `.zed/settings.json` 只影响该项目文件"的实现方式。

优先级(高 → 低):**项目本地(路径越深越高)> 服务器 > 用户 > 管理员全局 > 默认值**。用户设置内部还有三重内部覆盖:激活的 profile、发布渠道覆盖(如 `"nightly": {...}`)、操作系统覆盖(如 `"linux": {...}`)。

#### 4.2.2 核心流程

`recompute_values` 的全局合并顺序(后者覆盖前者):

```text
default(内嵌 default.json)
  ← extension(扩展贡献)
  ← global(管理员全局设置)
  ← user(用户 settings.json)
      ├─ user.content(主体)
      ├─ for_release_channel()(渠道覆盖,如 "dev"/"nightly")
      ├─ for_os()(平台覆盖,如 "linux"/"macos")
      └─ 激活 profile 的 settings(若 profile 的 base 是 User,
         上面三项才并入;profile settings 最后并入)
  ← server(SSH/远端场景由服务端下发)

随后对 local_settings 的每个 (worktree_id, 目录):
  从 merged_settings(或更浅目录的局部结果)出发
  ← 该目录的 .zed/settings.json
  → 存为该目录的局部值,供此目录下的文件读取
```

用一个式子表达单个字段的最终值(从低到高取第一个 `Some`):

\[ v_{\text{final}}(f, p) = \text{firstSome}\big(\, L_{d_k}(f),\ L_{d_{k-1}}(f),\ \dots,\ U(f),\ G(f),\ D(f) \,\big) \]

其中 \( p \) 是文件路径,\( d_1 \subset d_2 \subset \dots \subset d_k \) 是包含 \( p \) 且带有本地设置的目录(由浅到深),\( U/G/D \) 分别是用户/全局/默认层的值。

#### 4.2.3 源码精读

`SettingsStore` 的字段清单在 [settings_store.rs:L145-L168](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L145-L168),可以逐一对上前面说的分层:`default_settings`、`user_settings`、`global_settings`、`server_settings`、`extension_settings`、`local_settings: BTreeMap<(WorktreeId, Arc<RelPath>), SettingsContent>`、合并缓存 `merged_settings`,以及记录解析错误的 `file_errors` 和挂在 store 上的 `editorconfig_store` 实体。

优先级的正式定义是 `SettingsFile` 的 `Ord` 实现,[settings_store.rs:L186-L208](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L186-L208):

```rust
/// Sorted in order of precedence
impl Ord for SettingsFile {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        ...
        (Project(_), _) => Ordering::Less,   // Project 排最前 = 优先级最高
        (Server, _) => Ordering::Less,
        (User, _) => Ordering::Less,
        (Global, _) => Ordering::Less,       // Default 最低
    }
}
```

注意 `Project` 分支里 `rel_path1.cmp(rel_path2).reverse()`([L195-L197](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L195-L197)):路径"更大"(更深、更具体)的排更前——子目录的 `.zed/settings.json` 覆盖父目录的。

合并的核心 `recompute_values` 在 [settings_store.rs:L1334-L1455](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L1334-L1455)。全局段([L1343-L1375](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L1343-L1375))严格按"默认 ← 扩展 ← 全局 ← 用户(含渠道/OS/profile)← 服务器"的顺序调 `merge_from`:

```rust
let mut merged = self.default_settings.as_ref().clone();
merged.merge_from_option(self.extension_settings.as_deref());
merged.merge_from_option(self.global_settings.as_deref());
if let Some(user_settings) = self.user_settings.as_ref() {
    let active_profile = user_settings.for_profile(cx);
    ...
    merged.merge_from(&user_settings.content);
    merged.merge_from_option(user_settings.for_release_channel());
    merged.merge_from_option(user_settings.for_os());
    if let Some(profile) = active_profile {
        merged.merge_from(&profile.settings);
    }
}
merged.merge_from_option(self.server_settings.as_deref());
```

`merge_from` 的语义由 `MergeFrom` derive 生成([settings_macros.rs:L22-L81](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_macros/src/settings_macros.rs#L22-L81)):结构体逐字段调用 `merge_from`(对 `Option<T>` 即"对方是 `Some` 才覆盖");**枚举则整体替换**(`*self = other.clone()`,[L61-L65](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_macros/src/settings_macros.rs#L61-L65))——这就是 `GitGutterWidth` 这类枚举设置的覆盖规则:一旦某层写了,整条值替换,不存在"半继承"。

局部段([L1419-L1454](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L1419-L1454))用一个栈维护"当前目录的父链":遍历按 BTreeMap 顺序(同 worktree 内路径字典序)进行,遇到不再包含当前文件路径的目录就弹栈,然后把该目录的本地设置 `merge_from` 到栈顶(或全局值)之上,压栈并存为局部值。`disable_ai` 是个有趣的特例([L1364-L1373](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L1364-L1373)):它用"饱和或"逻辑把**所有**项目本地的 `disable_ai: true` 都汇入全局值——因为禁用 AI 是安全策略,任何项目都不能绕过。

合并完成后,对每个注册类型重新调用 `from_settings` 刷新全局值([L1377-L1380](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L1377-L1380))。这解释了一个工程约束:`from_settings` 必须廉价(克隆+解包),每次设置变化它都会被全量调用。

读取入口 [settings_store.rs:L446-L453](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L446-L453):

```rust
pub fn get<T: Settings>(&self, path: Option<SettingsLocation>) -> &T {
    self.setting_values
        .get(&TypeId::of::<T>())
        .unwrap_or_else(|| panic!("unregistered setting type {}", type_name::<T>()))
        .value_for_path(path)
        .downcast_ref::<T>()
        .expect("no default value for setting type")
}
```

按 `TypeId` 定位到该类型的值容器,`value_for_path` 在局部值列表里选**最深包含该路径**的一条,否则回落全局值。`SettingsLocation` 携带 `worktree_id + RelPath`([L139-L143](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L139-L143)),这就是语言设置能"按文件所在项目不同而不同"的机制基础。

最后是一句带过的 editorconfig 兼容:`EditorconfigStore`([editorconfig_store.rs:L48-L53](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/editorconfig_store.rs#L48-L53))作为实体挂在 `SettingsStore` 上,负责解析项目内外的 `.editorconfig`(基于 `ec4rs` 库),在配置文件变化时发出 `EditorconfigEvent::ExternalConfigChanged` 事件([L36-L42](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/editorconfig_store.rs#L36-L42)),供编辑器把 `indent_size`、`max_line_length` 等属性并入某个文件的最终生效值——它是"按文件叠加"这一思想的又一实现。

#### 4.2.4 代码实践

**实践:用纸笔推演一次三层覆盖(纯源码阅读型)**

1. **实践目标**:确认你真的理解了合并方向与目录栈。
2. **操作步骤**:
   - 假设有如下配置:默认 `git_gutter_width: "default"`;用户 settings.json 写 `"gutter": {"git_gutter_width": {"custom": 6.0}}`;项目 `/repo/.zed/settings.json` 写 `"gutter": {"git_gutter_width": "default"}`;项目子目录 `/repo/a/.zed/settings.json` 不存在。
   - 分别对文件 `/repo/a/main.rs` 和 `/repo/b/main.rs`(假设 `/repo/b/.zed/settings.json` 也写了 `{"custom": 8.0}`),按 `recompute_values` 的流程手推最终值。
3. **需要观察的现象**:你的推导过程应当复现"用户层先并入,再被项目层覆盖;目录栈按包含关系逐层叠加"。
4. **预期结果**:`/repo/a/main.rs` → `"default"`(项目根覆盖了用户层);`/repo/b/main.rs` → `{"custom": 8.0}`(b 目录自己的本地设置)。
5. 如需代码佐证,可在本地写一个使用 `FakeFs` + 临时 worktree 的测试来验证(参考 worktree crate 测试的组织方式),**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:服务器设置的优先级为什么比用户设置高?

答案:服务器设置用于 SSH/远端开发等场景,由服务端管理员下发。管理员全局策略(如统一禁用某功能)需要压过终端用户的个人偏好,否则策略无法生效。同理项目本地的 `disable_ai` 用 OR 逻辑汇入全局,也是策略优先的体现。

**练习 2**:为什么 `merged_settings` 要用 `Rc` 缓存,而不是每次读取时现算?

答案:读取是高频路径(渲染期间大量 `get_global`),现算需要重新克隆并合并多层 `SettingsContent`。`recompute_values` 只在设置变化时跑一次,把结果缓存进 `Rc<SettingsContent>`,读取变成一次指针解引用——典型的"写少读多,写时重算"。

**练习 3**:同一个 worktree 下 `/a/.zed/settings.json` 和 `/a/b/.zed/settings.json` 都存在时,文件 `/a/b/c.rs` 读到谁的局部值?

答案:`/a/b` 的。局部值按"最深包含该文件路径的目录"选择;`recompute_values` 中目录栈正是为了让 `/a/b` 的局部快照从 `/a` 的快照叠加而来(而非从全局值直接叠加),保证父目录的设置对子目录仍然生效、只被更深层的覆盖。

### 4.3 文件监听与热更新

#### 4.3.1 概念说明

"改 settings.json 保存后立即生效"不是魔法,而是一条清晰的事件链:

```text
用户保存 settings.json
  → fs 层产生文件事件(100ms 去抖合并)
  → watch_config_file 的循环重新读取文件全文并推入 channel
  → watch_settings_files 的前台任务收到新内容
  → SettingsStore::set_user_settings:内容去重 → 解析(先迁移)
  → recompute_values 全量重算
  → settings_changed 回调(记录解析错误/通知 UI)
  → cx.refresh_windows() 请求所有窗口重绘
  → 渲染代码里的 EditorSettings::get_global(cx) 读到新值
```

关键洞察:**设置没有自己的"订阅-通知"机制到每个视图**,它借用了 GPUI 的重绘循环——业务代码在渲染时同步读取设置值,所以"让所有窗口重绘一遍"就等价于"所有设置消费者都拿到新值"。这就是 `refresh_windows` 一行就能实现热更新的原因。

去抖(100ms)则解决编辑器自动保存、格式化工具等连续写文件导致的重复解析;`canonicalize` 解决配置目录是符号链接(如 dotfiles 管理)时监听错路径的问题。

#### 4.3.2 核心流程

见上面的链路图。补充两个细节:

- 初次启动时,`watch_settings_files` 会**同步**(`block_on`)读取一次用户与全局设置,保证应用完成初始化前设置已就位;之后的更新走异步循环。
- 两个监听流(user 与 global 文件)用 `futures::stream::select` 合并成一条,哪个文件变了就按 `SettingsFile` 标记分发对应的 `set_*_settings`。

#### 4.3.3 源码精读

文件级监听 `watch_config_file` 在 [settings_file.rs:L171-L200](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_file.rs#L171-L200):

```rust
pub fn watch_config_file(
    executor: &BackgroundExecutor,
    fs: Arc<dyn Fs>,
    path: PathBuf,
) -> (mpsc::UnboundedReceiver<String>, gpui::Task<()>) {
    let (tx, rx) = mpsc::unbounded();
    let task = executor.spawn(async move {
        let path = fs.canonicalize(&path).await.unwrap_or_else(|_| path);
        let (events, _) = fs.watch(&path, Duration::from_millis(100)).await;
        futures::pin_mut!(events);

        let contents = fs.load(&path).await.unwrap_or_default();
        if tx.unbounded_send(contents).is_err() { return; }

        loop {
            if events.next().await.is_none() { break; }
            if let Ok(contents) = fs.load(&path).await
                && tx.unbounded_send(contents).is_err() { break; }
        }
    });
    (rx, task)
}
```

三个要点:`canonicalize` 在监听前解析符号链接(有专门测试覆盖这种场景,[settings_file.rs:L62-L95](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_file.rs#L62-L95));`fs.watch` 的第二参数就是 100ms 去抖窗口;事件到达后**重新读取全文**(而非增量),把内容字符串推给前台。它运行在后台执行器上,读文件这类阻塞 IO 不占用 UI 线程(并发模型回顾 u2-l6)。

组装层 `watch_settings_files` 在 [settings_store.rs:L353-L404](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L353-L404)。先是两路监听与首次同步读取([L359-L382](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L359-L382)):

```rust
let (mut user_settings_file_rx, user_settings_watcher) =
    crate::watch_config_file(cx.background_executor(), fs.clone(), paths::settings_file().clone());
let (mut global_settings_file_rx, global_settings_watcher) =
    crate::watch_config_file(cx.background_executor(), fs, paths::global_settings_file().clone());

let global_content = cx.foreground_executor().block_on(global_settings_file_rx.next()).unwrap();
let user_content = cx.foreground_executor().block_on(user_settings_file_rx.next()).unwrap();
```

然后是常驻的前台更新循环([L384-L403](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L384-L403)):

```rust
self._settings_files_watcher = Some(cx.spawn(async move |cx| {
    let mut settings_streams = futures::stream::select(
        global_settings_file_rx.map(|content| (SettingsFile::Global, content)),
        user_settings_file_rx.map(|content| (SettingsFile::User, content)),
    );
    while let Some((settings_file, content)) = settings_streams.next().await {
        cx.update_global(|store: &mut SettingsStore, cx| {
            let result = match settings_file {
                SettingsFile::User => store.set_user_settings(&content, cx),
                SettingsFile::Global => store.set_global_settings(&content, cx),
                _ => return,
            };
            settings_changed(settings_file, result, cx);
            cx.refresh_windows();
        });
    }
}));
```

每条新内容进入 `set_user_settings`([settings_store.rs:L928-L951](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L928-L951)):先用 `last_user_settings_content` 与上次内容比对,相同则直接返回 `Unchanged`(去抖之后的双保险);不同则走"解析+迁移"(下一节)并 `recompute_values`。最后 `cx.refresh_windows()` 收尾。watcher 任务存进 `_settings_files_watcher` 字段——按 GPUI 的任务语义,store 这个 Global 永不销毁,任务也就常驻。

#### 4.3.4 代码实践

**实践:亲手触发热更新(本地实验)**

1. **实践目标**:观察完整热更新链路的用户可见效果。
2. **操作步骤**:
   - 从源码运行 Zed(`cargo run`,构建方式见 u1-l2)。
   - 打开 `~/.config/zed/settings.json`(Linux 路径,其他平台见 `paths` crate),添加 `"gutter": { "git_gutter_width": { "custom": 20.0 } }` 并保存。
   - 观察任意打开的编辑器视图 gutter 中 git diff 条的宽度;再把值改成 `2.0`、"default" 各保存一次。
3. **需要观察的现象**:每次保存后约 100ms~1s 内,gutter 宽度变化,**全程无需重启**;改成 `"default"` 时宽度随行高缩放。
4. **预期结果**:如上所述。若想看得更清楚,可同时在设置里故意写一个类型错误的值(如 `"git_gutter_width": true`),观察该字段被忽略而其余设置仍生效(`with_fallible_options` 的容错)。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:为什么每次文件事件后是重读全文推送字符串,而不是推一个"文件变了"的标志?

答案:监听运行在后台执行器,无法安全地触碰 GPUI 的实体与全局状态;而 `set_user_settings` 必须在 `cx.update_global` 里执行。把 IO 产品(文件内容字符串)通过 channel 递给前台,是 GPUI"后台做 IO、前台改状态"分工的自然结果。

**练习 2**:如果去掉 `refresh_windows()`,设置值还会更新吗?界面呢?

答案:值会更新——`recompute_values` 已经重算了 store 内部状态;但界面不会变,因为渲染代码只有在重绘时才重新调用 `get_global` 读取设置。没有重绘,旧像素就一直留在屏幕上。

**练习 3**:100ms 去抖解决什么问题?去掉会有什么后果?

答案:把同一文件在极短时间内的多次事件(如自动保存、格式化重写)合并成一次重读与重算。去掉会导致每次事件都触发一次"读文件 + 解析 + 全量重算 + 全窗口重绘",在连续写入时造成无谓的 CPU 与 IO 开销。

### 4.4 设置迁移:migrator 如何改写旧配置

#### 4.4.1 概念说明

设置系统的 JSON 格式会演进:键改名、值从数组变字符串、布尔升级为枚举……老用户的 `settings.json` 里还写着旧格式。三种糟糕的处理方式是:让用户手改(体验差)、旧值解析失败(设置全部丢失)、静默丢弃旧键。Zed 的答案是 **migrator crate**:在解析之前,对原始 JSON **文本**跑一串按日期命名、只增不改的迁移函数,把旧格式机械地改写成新格式。

两条铁律写在 [migrator.rs:L1-L15](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs#L1-L15) 的模块文档里:

1. **绝不修改历史迁移,只新增**。因为迁移是链式执行的,内部状态保证"每个用户的配置都已被迁到上一版",你只需要写"上一版 → 当前版"这一步;改历史迁移会破坏从更老版本连续升级的路径。
2. **迁移要幂等**:对新格式配置再跑一遍必须无变化(测试里专门断言了这一点)。

迁移有两类(定义见 [migrator.rs:L154-L157](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs#L154-L157)):

- **TreeSitter 型**:用 tree-sitter 解析 JSON 语法树,按查询模式定位,输出文本编辑区间——能**保留用户的注释与格式**;
- **Json 型**:整体解析成 `serde_json::Value`,回调改写,再通过 `update_value_in_json_text` 差异回写成文本编辑。

本讲案例 `git_gutter_width`(旧:数字 `6.0`;新:`"default"` 或 `{"custom": 6.0}`)用的是 **Json 型**,改动幅度小且需要按值类型分派,操作语义化的 Value 比写语法树查询更直接。

迁移的挂载点在设置加载路径上:[settings_store.rs:L787-L818](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L787-L818) 的 `parse_and_migrate_zed_settings` 在每次 `set_user_settings`/`set_global_settings` 时调用 `migrator::migrate_settings(text)`。三态结果映射到 `MigrationStatus`([L1467-L1474](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L1467-L1474)):

- `Ok(None)` → `NotNeeded`:已是最新,原文本直接解析;
- `Ok(Some(new_text))` → `Succeeded`:**内存中**按新文本解析生效,但磁盘文件还是旧的——设置 UI 会显示横幅提示用户确认把新文本写回文件([settings_ui.rs:L3859-L3860](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_ui/src/settings_ui.rs#L3859-L3860));
- `Err` → `Failed`:迁移失败,回退用原始文本解析(错误信息随 `file_errors` 记录)。

#### 4.4.2 核心流程

一条旧配置 `"git_gutter_width": 6.0` 的迁移之旅:

```text
set_user_settings(旧文本)
  → parse_and_migrate_zed_settings
      → migrator::migrate_settings(text)          # 依次执行全部迁移
          → ... 此前的 30 余条迁移(无命中,原样通过)
          → m_2026_08_17::make_git_gutter_width_an_enum
              → migrations::migrate_settings(value, migrate_one)
                  → migrate_one(root 对象)
                  → migrate_one(每个发布渠道覆盖对象)
                  → migrate_one(每个平台覆盖对象)
                  → migrate_one(每个 profile 的 settings 对象)
                      # migrate_one 内部:gutter.git_gutter_width 是数字
                      #   → 替换为 {"custom": 原数字}
              → 新旧 Value 不同 → update_value_in_json_text 生成文本编辑
          → 产出新文本
  → MigrationStatus::Succeeded,用新文本解析出 UserSettingsContent
  → recompute_values → refresh_windows(界面立即按新格式生效)
  → 设置 UI 提示:你的 settings.json 已在内存中迁移,是否写回磁盘?
```

#### 4.4.3 源码精读

迁移案例本体只有 30 行,[m_2026_08_17/settings.rs:L6-L32](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrations/m_2026_08_17/settings.rs#L6-L32):

```rust
pub fn make_git_gutter_width_an_enum(value: &mut Value) -> Result<()> {
    migrate_settings(value, &mut migrate_one)
}

fn migrate_one(obj: &mut serde_json::Map<String, Value>) -> Result<()> {
    let Some(gutter) = obj.get_mut("gutter").and_then(|gutter| gutter.as_object_mut()) else {
        return Ok(());
    };
    let Some(git_gutter_width) = gutter.get_mut("git_gutter_width") else {
        return Ok(());
    };
    *git_gutter_width = match git_gutter_width {
        Value::Number(n) => serde_json::json!({ "custom": n }),
        _ => return Ok(()),
    };
    Ok(())
}
```

`migrate_one` 只处理**一个**设置对象:没有 `gutter` 键、没有 `git_gutter_width` 键、值不是数字,都静默返回(幂等的来源——新格式的值是字符串或对象,永远不会命中 `Value::Number` 分支)。真正让它覆盖所有作用域的是 `migrations::migrate_settings` 辅助函数,[migrations.rs:L7-L48](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrations.rs#L7-L48):它对根对象、每个发布渠道覆盖(`ReleaseChannelOverrides::OVERRIDE_KEYS`)、每个平台覆盖(`PlatformOverrides::OVERRIDE_KEYS`)、每个 profile(优先取 `settings` 内层,兼容旧形态)分别调用 `migrate_one`。也就是说,**写迁移的人只需要关心字段怎么变,作用域遍历是现成的**。

新迁移要登记进执行清单的最后,[migrator.rs:L259-L261](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs#L259-L261):

```rust
        MigrationType::TreeSitter(
            migrations::m_2026_05_04::SETTINGS_PATTERNS,
            &SETTINGS_QUERY_2026_05_04,
        ),
        MigrationType::Json(migrations::m_2026_08_17::make_git_gutter_width_an_enum),
    ];
```

整个清单([L159-L263](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs#L159-L263))按时间从旧到新排列,`run_migrations`([L71-L118](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs#L71-L118))依次应用:每条迁移拿当前文本,可能产出新文本作为下一条的输入——这就是"从 2025 年 1 月的格式一路链式升到今天"的实现。Json 型迁移在值无变化时返回 `None`,所以对已迁移的文件是空操作。

测试约定也值得学:[migrator.rs:L441-L450](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs#L441-L450) 的 `assert_migrate_settings` 断言迁移结果后,还会**对结果再跑一遍并断言无变化**(幂等性)。本案例的测试见 [L5428-L5451](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs#L5428-L5451)(`4.0 → {"custom": 4.0}`)与 [L5454-L5483](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs#L5454-L5483)(`"default"`、`{"custom": 4.0}`、无 gutter 键三种"不该动"的情况)。

最后,Json 型迁移的文本回写不走"重新序列化整份 JSON"(那会毁掉注释与格式),而是比较改写前后的两棵 Value 树,用 `update_value_in_json_text` 生成**局部文本编辑**([migrator.rs:L82-L110](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs#L82-L110))——用户的注释、缩进、键顺序在迁移后原样保留。

#### 4.4.4 代码实践

**实践:亲手跑一次迁移(测试驱动,可本地验证)**

1. **实践目标**:在 migrator 的测试里直接观察旧值被改写的过程。
2. **操作步骤**:
   - 在 [migrator.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/migrator/src/migrator.rs) 的 `tests` 模块里,模仿 `test_make_git_gutter_width_an_enum_from_number`,新建一个测试:输入包含 `"gutter": { "git_gutter_width": 2.0 }` 的 JSON(可以再故意加一行注释 `// my note`),断言 `assert_migrate_settings` 输出为 `{"custom": 2.0}` 且注释仍在。
   - 运行:`cargo test -p migrator git_gutter`。
3. **需要观察的现象**:迁移输出把数字包进 `{"custom": ...}`,你写的注释原样保留;幂等断言(重跑无变化)通过。
4. **预期结果**:测试通过。若想观察"平台覆盖也被迁移",可把输入包一层 `"linux": { ... }` 再断言,**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:假设要把旧布尔设置 `"auto_indent": true` 迁移为枚举字符串 `"syntax_aware"`,迁移函数的 `migrate_one` 怎么写?

答案:与 `m_2026_08_17` 同构——拿到根对象后 `match obj.get_mut("auto_indent")`:`Value::Bool(true)` 替换为 `json!("syntax_aware")`,`Value::Bool(false)` 替换为 `json!("none")`(按语义映射),其他值(已是字符串等)原样返回。再包一层 `migrations::migrate_settings(value, &mut migrate_one)` 覆盖平台/渠道/profile 作用域。真实实现可参照 `m_2025_01_27::make_auto_indent_an_enum`,它正是这么做的。

**练习 2**:为什么迁移必须幂等?什么场景会暴露非幂等的 bug?

答案:迁移在**每次**加载设置文件时都会跑(不只升级时)。若不幂等,新格式值会被反复改写——轻则每次启动都提示"文件已迁移待写回",重则值在两种形态间来回翻转。测试辅助函数专门对结果重跑一遍断言无变化,就是防这个。

**练习 3**:为什么迁移改写的是 JSON **文本**而不是"解析→改→重新序列化整份文件"?

答案:用户的 settings.json 里有注释、特定缩进和键顺序;整份重新序列化会把这些全部抹掉。Json 型迁移比较改写前后的 Value 树、只对变化的子树生成局部文本编辑(`update_value_in_json_text`);TreeSitter 型则直接在语法树上算编辑区间。两条路都是为了"只动该动的那几个字符"。

## 5. 综合实践

**任务:给 Zed 加一个 `show_line_count` 设置——从定义、默认值、消费到热更新的完整闭环,再写一个配套迁移思路。**

前置:完成 u1-l2 的构建环境。以下代码均为**示例代码**(基于本讲读到的真实模式编写,非仓库现有代码),改动量小,适合作为第一次贡献的演练。

**第一步:内容层加字段。** 在 `crates/settings_content/src/settings_content.rs` 的 `SettingsContent` 结构体里(参照 `vim_mode` 字段的位置,约 [L302-L305](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/settings_content.rs#L302-L305) 附近)加:

```rust
/// Whether to show the total line count of the active buffer in the status bar.
///
/// Default: false
pub show_line_count: Option<bool>,
```

结构体上的 `#[with_fallible_options]` 与 `derive(MergeFrom, ...)` 会自动覆盖新字段。

**第二步:补默认值。** 在 `assets/settings/default.json` 根级加 `"show_line_count": false`。忘了这步,启动时 `unwrap()` 会 panic——先忘一次再补上,正好体会这个设计。

**第三步:定义强类型设置。** 状态栏在 workspace crate,把设置也放那里(仿照 [vim_mode_setting.rs:L10-L17](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/vim_mode_setting/src/vim_mode_setting.rs#L10-L17) 的形态):

```rust
use settings::{RegisterSetting, Settings, SettingsContent};

#[derive(RegisterSetting)]
pub struct ShowLineCountSetting(pub bool);

impl Settings for ShowLineCountSetting {
    fn from_settings(content: &SettingsContent) -> Self {
        Self(content.show_line_count.unwrap())
    }
}
```

**第四步:在状态栏消费。** 找到 `crates/workspace/src/status_bar.rs` 中渲染右侧状态项的代码,在某个现有元素的渲染处按开关包裹(示意):

```rust
if ShowLineCountSetting::get_global(cx).0 {
    // 从活动编辑器快照取行数渲染,例如 buffer_snapshot().max_point().row() + 1
}
```

行数获取的具体 API 可参考 u5-l1 讲过的 `EditorSnapshot`/`MultiBuffer` 路径,此处以跑通设置链路为主。

**第五步:验证热更新。** `cargo run` 启动,在 `~/.config/zed/settings.json` 加 `"show_line_count": true` 保存——状态栏应立即出现行数,改回 `false` 消失,全程不重启。**待本地验证**。

**第六步(迁移思路演练)**:假设第二天的 PR 把 `show_line_count: true/false` 升级为枚举 `"always" | "never"`,写出迁移函数思路:

- 新建 `crates/migrator/src/migrations/m_<今日日期>/settings.rs`,导出 `make_show_line_count_an_enum(value: &mut Value) -> Result<()>`;
- `migrate_one` 里 `match obj.get_mut("show_line_count")`:`Value::Bool(true)` → `json!("always")`,`Value::Bool(false)` → `json!("never")`,其余返回不动;
- 用 `crate::migrations::migrate_settings(value, &mut migrate_one)` 包住,自动覆盖渠道/平台/profile;
- 在 `migrations.rs` 加对应模块、在 `migrator.rs` 的清单末尾登记 `MigrationType::Json(...)`;
- 补测试:旧值迁移、新值不动、无键不动,复用 `assert_migrate_settings` 自带的幂等断言。

对照 `m_2026_08_17` 的真实实现逐条核对你的思路,差异就是你要补的细节。

## 6. 本讲小结

- 设置系统三层分工:`settings_content` 定义 JSON 形态(字段全 `Option`,None=继承),业务 crate 用 `#[derive(RegisterSetting)]` + `Settings::from_settings` 定义强类型消费视图,`inventory` 让注册零中心化。
- `SettingsStore` 是唯一权威 Global,维护默认/扩展/全局/用户/服务器/项目本地多份内容;`recompute_values` 按"默认←扩展←全局←用户(含渠道/OS/profile)←服务器"合并出全局值,再按目录栈为每个含本地设置的目录叠加局部值;读取时 `get(Some(location))` 选最深包含路径的局部值。
- 热更新链路:后台 `watch_config_file`(canonicalize + 100ms 去抖)重读全文 → 前台 `set_user_settings` 去重、解析、重算 → `cx.refresh_windows()`;界面能即时生效是因为渲染代码在重绘时同步读设置,重绘即刷新。
- 格式演进靠 migrator:解析前对原始文本跑按日期排序、只增不改、必须幂等的迁移链;`MigrationStatus::Succeeded` 表示内存已迁移而磁盘待用户确认写回;Json 型迁移经由 `migrate_settings` 辅助自动覆盖渠道/平台/profile 作用域。
- `git_gutter_width` 案例串起了全部环节:内容层 `GitGutterWidth` 枚举(`Default` 随行高 0.275 倍缩放 / `Custom(PixelSetting)` 固定像素)、`default.json` 默认 `"default"`、消费层 `Gutter` 结构 unwrap 收口、渲染层 `match` 分派,以及把旧数值 `n` 改写为 `{"custom": n}` 的 `m_2026_08_17` 迁移。

## 7. 下一步学习建议

- **u7-l2(Keymap 与命令面板)**:同一套"JSON 文件 + 监听 + 迁移"的模式在键位上的翻版,`KeymapFile` 与本讲的 `watch_config_file` 关系密切。
- **u7-l3(扩展系统)**:扩展如何向 `SettingsStore` 贡献 `extension_settings` 层,以及扩展自带设置的 schema 校验。
- 想加深分层读取的理解,可预读 **u6-l1** 会用到的 `LanguageSettings`——它是"按 `SettingsLocation` 取局部值"的最重度用户,同一文件在不同项目可以有不同的 tab_size。
- 源码延伸阅读:`crates/settings_json`(迁移与 UI 编辑共用的"保留格式的 JSON 文本更新"工具)与 `crates/settings_ui`(设置编辑页面,看 `GitGutterWidth` 如何出现在 Settings UI 的下拉与数字输入中)。
