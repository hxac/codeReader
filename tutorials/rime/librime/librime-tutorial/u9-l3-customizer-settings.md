# Customizer 与用户设置

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「用户想改配置，又不想污染发行版官方文件」这条需求在 librime 中是如何被满足的——即 `.custom.yaml` 增量定制机制。
- 区分三条互补却又常被混淆的代码路径：被废弃的 `Customizer`（就地改写官方文件）、现代的 `CustomSettings`（只负责读写 `.custom.yaml`）、以及真正把补丁合并进配置的入口 `AutoPatchConfigPlugin` + `ConfigFileUpdate`。
- 掌握 `CustomSettings` 的 `Load / Customize / Save / IsFirstRun` 四个方法如何操纵「官方配置 + 补丁文件」这两份独立的 `Config`。
- 理解 `SwitcherSettings` 如何在 `CustomSettings` 之上，把「选哪几个方案」翻译成对 `default.custom.yaml` 的 `schema_list` 补丁。
- 了解 `UserDictManager` 作为用户词典的备份、导出、同步、升级门面，与配置定制是平行的「用户数据」管理职责。

## 2. 前置知识

本讲承接 u9-l2（部署任务族）。需要先建立这些概念：

- **`*.custom.yaml`**：与某个官方配置同名的「补丁文件」。`luna_pinyin.schema.yaml` 对应 `luna_pinyin.custom.yaml`，`default.yaml` 对应 `default.custom.yaml`。它放在用户目录里，只写「要改的键」。
- **`patch:` 与 `__patch:`**：补丁 DSL 的两种写法。`.custom.yaml` 顶层用小写 `patch:` 承载一条条「路径→值」；`__patch`（双下划线）是配置编译器内部的指令（见 u4-l3）。本讲会讲清二者如何由 `AutoPatchConfigPlugin` 衔接。
- **五个部署目录**：`shared_data_dir`（发行版官方，只读）、`user_data_dir`（用户可写）、`prebuilt_data_dir`、`staging_dir`（编译产物落点）、`sync_dir`（同步中转）。回顾 u9-l1。
- **配置数据模型**：`Config` / `ConfigItem` / `ConfigMap` / `ConfigList` / `ConfigValue`（u4-l1）。
- **`config` 组件 vs `config_builder` 组件**：前者运行期直接读 YAML、不支持 DSL；后者部署期经 `ConfigCompiler` 支持 `__include/__patch` DSL（u4-l2、u9-l2）。本讲的合并只发生在后者。
- **`RimeLeversApi`**：`levers` 模块通过 `get_api` 暴露的一组 C 方法，把本讲的四个 C++ 类包装成跨语言接口。

一句话直觉：librime 把「官方配置」与「用户定制」**物理分离**——官方文件只读、来自 `shared_data_dir`；用户的全部修改都落到 `user_data_dir` 里一个独立的 `.custom.yaml` 补丁文件；**部署期**由配置编译器把两者合并，产物写到 `staging_dir`。于是升级官方方案永远不会冲掉用户的改动。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/rime/lever/customizer.h` / `.cc` | （已废弃）就地改写目标配置、逐键应用 `patch` 的旧实现。 |
| `src/rime/lever/custom_settings.h` / `.cc` | 现代定制 API：加载官方配置 + 读写 `.custom.yaml`。 |
| `src/rime/lever/switcher_settings.h` / `.cc` | 继承 `CustomSettings`，管理方案清单与切换器热键。 |
| `src/rime/lever/user_dict_manager.h` / `.cc` | 用户词典的列举/备份/恢复/导出/导入/同步/升级。 |
| `src/rime/config/auto_patch_config_plugin.cc` | 编译期插件，自动把 `.custom.yaml` 的 `patch:` 挂成 `__patch` 依赖——**合并的真正发生地**。 |
| `src/rime/lever/deployment_tasks.cc` | `ConfigFileUpdate` / `SchemaUpdate` 在部署期触发 `config_builder` 重编译。 |
| `src/rime/lever/levers_api_impl.h` | C API 包装：`custom_settings_init` / `customize_item` / `switcher_settings_init` / 用户词典族。 |
| `src/rime/signature.cc` | 给 `.custom.yaml` 盖「生成者 / 修改时间」签名。 |
| `tools/rime_patch.cc` | 命令行工具，用 levers API 写一条 patch。 |
| `data/minimal/default.yaml` | 全局默认配置，是 `default.custom.yaml` 的补丁对象，也是切换器读取的 `schema_list` 来源。 |

## 4. 核心概念与源码讲解

### 4.1 全景：.custom.yaml 如何生效

#### 4.1.1 概念说明

需求很具体：用户想改某方案的每页候选数、想加一个 filter、想换默认方案清单。但官方 `*.schema.yaml` / `default.yaml` 来自发行版，升级时会被覆盖。解法是「增量补丁」——用户在 `user_data_dir` 放一个 `<config_id>.custom.yaml`，里面只写要改的键。

要紧的是理解：**这个补丁不是在「读取配置」时合并，而是在「部署期编译配置」时合并。** 整条链路分两段独立的代码：

1. **维护补丁文件**（写）：用户手写，或由 `rime_patch` 工具 / levers API / `CustomSettings` 程序化生成 `<config_id>.custom.yaml`，其顶层是 `patch:` 映射。
2. **合并补丁**（读并应用）：部署期 `ConfigFileUpdate` 发现源文件变了（靠 `__build_info/timestamps` 判断），改用 `config_builder` 重编译；`AutoPatchConfigPlugin` 自动给每个资源挂一条「引用 `<config_id>.custom:/patch`」的 `__patch` 依赖；编译器求解依赖，把 `patch:` 里的每条「路径→值」合并进配置树，产物落到 `staging_dir`。

运行期 `config` 组件从 `staging_dir` 读到的，就是已经合并了用户补丁的最终配置。**4.2 的 `CustomSettings` 只负责第 1 段；第 2 段归配置编译器（u4-l3、u4-l4）。** 把这两段混为一谈，是读这批代码最常见的误区。

#### 4.1.2 核心流程

```
[用户]  写 default.custom.yaml / luna_pinyin.custom.yaml
            │  顶层 patch: { "menu/page_size": 9, ... }
            ▼
[部署]  start_maintenance → WorkspaceUpdate
            │  对 default.yaml 跑 ConfigFileUpdate
            │  对每个方案跑 SchemaUpdate → ConfigFileUpdate
            ▼
[编译]  ConfigFileUpdate: ConfigNeedsUpdate ? → 用 config_builder 重编译
            │  AutoPatchConfigPlugin.ReviewCompileOutput
            │    注入 __patch: <id>.custom:/patch?   (optional)
            ▼
[产物]  staging_dir/default.yaml、<schema>.schema.yaml（已含用户补丁）
            ▼
[运行]  config 组件从 staging 读 → Engine 装配流水线
```

#### 4.1.3 源码精读

合并的真正发生地是 `AutoPatchConfigPlugin::ReviewCompileOutput`：

[auto_patch_config_plugin.cc:19-36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/auto_patch_config_plugin.cc#L19-L36) — 编译期，对每个非 `.custom` 资源，若根节点没有显式 `__patch`，就自动追加一条指向 `<id>.custom:/patch` 的可选 `PatchReference` 依赖。三处关键判断：

- [auto_patch_config_plugin.cc:21-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/auto_patch_config_plugin.cc#L21-L22) 跳过 `.custom` 资源本身，避免递归自引用。
- [auto_patch_config_plugin.cc:23-26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/auto_patch_config_plugin.cc#L23-L26) 若根节点已显式写了 `__patch`，就不重复自动挂，防止双重 patch。
- [auto_patch_config_plugin.cc:27-33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/auto_patch_config_plugin.cc#L27-L33) 把资源名去掉 `.schema` 后缀拼成 `<id>.custom`，以 `patch` 为子路径、`optional=true`（找不到补丁也不报错）构造引用，登记为依赖。

触发重编译的入口在部署任务里：

[deployment_tasks.cc:431-452](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L431-L452) — `ConfigFileUpdate::Run`：先用 `config` 组件加载，若 `ConfigNeedsUpdate`（`__build_info/timestamps` 与源文件 mtime 不符）则改用 `config_builder` 重编译并落 staging。

[deployment_tasks.cc:342-346](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L342-L346) — `SchemaUpdate::Run` 对每个方案先跑一次 `ConfigFileUpdate(schema_id + ".schema.yaml", "schema/version")`，这正是方案级补丁 `luna_pinyin.custom.yaml` 被合并的触发点。

[deployment_tasks.cc:170-171](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L170-L171) — `WorkspaceUpdate` 先对 `default.yaml` 跑 `ConfigFileUpdate`，于是 `default.custom.yaml` 在此合并。

> **对照：被废弃的就地改写。** `Customizer::UpdateConfigFile` 是这套机制的老前身——它直接把官方文件拷到用户目录、把 `patch` 映射逐键 `SetItem` 覆盖进目标文件本体、并用 `.custom.<checksum>` 后缀标记版本。[customizer.cc:18-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/customizer.cc#L18-L30) 用一张「source / custom / dest 版本」九宫格说明更新判定；[customizer.cc:114-123](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/customizer.cc#L114-L123) 是逐键打补丁的循环；[customizer.h:21-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/customizer.h#L21-L22) 明确标注 `DEPRECATED: in favor of auto-patch config compiler plugin`。新机制不再改写官方文件本体，而是靠编译器在 staging 区产出合并结果，`Customizer` 由此退出舞台——但理解它，能帮你看清「为什么现在的做法更好」。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `auto-patch` 与 `config_builder` 在部署期被触发，并验证 staging 产物含用户补丁。

**操作步骤**：

1. 在一个干净的 `user_data_dir` 放置 `data/minimal` 提供的 `default.yaml`、`luna_pinyin.schema.yaml`、`luna_pinyin.dict.yaml`。
2. 追加一个 `default.custom.yaml`（示例代码）：
   ```yaml
   patch:
     "menu/page_size": 9
   ```
3. 编译并运行 `rime_api_console`（见 u1-l5），首次启动会自动 `start_maintenance`。
4. 打开 glog 输出，搜索关键字 `auto-patch` 与 `updating config file`。

**需要观察的现象**：日志里出现形如 `auto-patch default:/__patch: default.custom:/patch?` 的行，随后 staging 目录下生成的 `default.yaml` 中 `menu/page_size` 变为 9。

**预期结果**：staging 区 `default.yaml` 的 `menu/page_size` 已被补丁覆盖为 9。若没生效，多半是源文件 mtime 不新于 `__build_info/timestamps`、未触发 `config_builder`——删掉 staging 产物强制重建即可。若本地无法运行，标注「待本地验证」，改为阅读 `AutoPatchConfigPlugin` 与 `ConfigFileUpdate` 源码确认链路。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `page_size` 写进 `luna_pinyin.custom.yaml` 在升级发行版后依然有效，而直接改 `luna_pinyin.schema.yaml` 会被冲掉？

**参考答案**：`.custom.yaml` 是独立于官方文件的用户文件，放在 `user_data_dir`；发行版升级只覆盖 `shared_data_dir` 的官方文件，不会动用户的 `.custom.yaml`。合并发生在 staging 区，产物每次部署都重建，所以补丁恒久生效。

**练习 2**：`AutoPatchConfigPlugin` 为什么要跳过「根节点已有显式 `__patch`」的资源？

**参考答案**：避免对同一份配置重复打补丁。用户若已在配置里显式写了 `__patch`，说明要自行控制补丁来源；自动再挂一条会与之叠加或冲突。

### 4.2 CustomSettings：定制文件读写 API

#### 4.2.1 概念说明

`CustomSettings` 是 4.1 链路中「维护 `.custom.yaml`」这一半的运行期 API。它持有**两份** `Config`：

- `config_`：从 staging / prebuilt 读到的**官方配置**，用来查询「当前值是什么」。
- `custom_config_`：从 `user_data_dir` 读到的 `.custom.yaml` **补丁文件**，用来读写用户的修改。

关键认知：它**不负责把补丁合并进 `config_`**——合并是编译期的事。它只编辑补丁文件本身。`GetValue` 查的是官方值，不是补丁后的值。

它是 `SwitcherSettings` 的基类，也被 levers C API 直接包装成 `RimeCustomSettings`，是 `rime_patch` 工具的后端。

#### 4.2.2 核心流程

```
构造 CustomSettings(deployer, config_id, generator_id)
   │
Load():
   ├─ config_       ← staging_dir/<config_id>.yaml （失败则回退 prebuilt_data_dir）
   └─ custom_config_ ← user_data_dir/<config_id>.custom.yaml （没有则 warning）
       （二者独立加载，互不合并）
   │
Customize(key, item):
   ├─ custom_config_["patch"][key] = item
   └─ modified_ = true
   │
Save():   （仅当 modified_ 为真）
   ├─ Signature(generator_id_, "customization").Sign(custom_config_)
   └─ custom_config_.SaveToFile(user_data_dir/<config_id>.custom.yaml)
```

#### 4.2.3 源码精读

[custom_settings.h:16-40](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.h#L16-L40) — 类声明。注意 [custom_settings.h:36-39](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.h#L36-L39) 的两个 `Config` 成员：`config_`（官方）与 `custom_config_`（补丁）；`generator_id_` 用来给签名区分来源（如 `"rime_patch"`、`"Rime::SwitcherSettings"`）。

`<config_id>` 到补丁文件名的映射是约定俗成的「剥后缀」：

[custom_settings.cc:21-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.cc#L21-L23) — `custom_config_file`：去掉 `.schema` 后缀再加 `.custom.yaml`。故 `luna_pinyin.schema` → `luna_pinyin.custom.yaml`，`default` → `default.custom.yaml`。

`Load` 的两段式加载：

[custom_settings.cc:30-47](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.cc#L30-L47) — `config_` 先试 staging、再回退 prebuilt（L31-37）；`custom_config_` 从 user_data_dir 读补丁文件，读不到只是 warning 并返回 false（L38-45）。**全程不把 `custom_config_` 的 `patch` 应用到 `config_`**。

`Customize` 只改补丁文件的 `patch:` 映射：

[custom_settings.cc:73-84](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.cc#L73-L84) — 取/建 `patch` 这个 `ConfigMap`，`Set(key, item)`，再把整个 `patch` 分支整体 `SetItem` 回去。注意 [custom_settings.cc:79-81](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.cc#L79-L81) 的注释解释了为何要整体写回：`key` 可能含斜杠（如 `menu/page_size`），`ConfigMap::Set` 会把它当成单层字面键名，无法直接定位子项，故必须整支写回由保存逻辑正确序列化。最后置 `modified_=true`。

`Save` 盖签名后落盘：

[custom_settings.cc:49-59](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.cc#L49-L59) — 用 `Signature(generator_id_, "customization")` 给补丁文件打上 `customization/generator`、`customization/modified_time` 等字段（签名实现见 [signature.cc:15-29](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/signature.cc#L15-L29)），再写到 user_data_dir。

`IsFirstRun` 判断该配置是否「还没被任何生成器定制过」：

[custom_settings.cc:86-93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.cc#L86-L93) — 若补丁文件不存在，或存在但缺少 `customization` 映射（即没被 `Save` 盖过签名），返回 true。前端常用它决定是否弹「首次配置」向导。

C API 包装层（`rime_patch` 工具就是走这条路）：

[levers_api_impl.h:12-17](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_api_impl.h#L12-L17) — `custom_settings_init` 把 `CustomSettings` 包成不透明的 `RimeCustomSettings*`。
[levers_api_impl.h:63-74](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_api_impl.h#L63-L74) — `customize_item` 把一个 `RimeConfig`（已被 `config_load_string` 解析）的根节点取出来，交给 `CustomSettings::Customize`。

[rime_patch.cc:20-52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_patch.cc#L20-L52) — 工具完整流程：`custom_settings_init → load_settings → customize_item(key, yaml) → save_settings → destroy`。

#### 4.2.4 代码实践

**实践目标**：用 `rime_patch` 给 `default` 改 `page_size`，验证它**只**动了 `default.custom.yaml`、没动 `default.yaml`。

**操作步骤**：

1. 记录 `user_data_dir/default.yaml` 的校验和（如 `sha256sum`）。
2. 运行（用法见 [rime_patch.cc:11-16](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_patch.cc#L11-L16)）：
   ```bash
   rime_patch default "menu/page_size" 9
   ```
3. 查看 `user_data_dir/default.custom.yaml` 内容；再次计算 `default.yaml` 的校验和。

**需要观察的现象**：`default.custom.yaml` 被写入为（示例代码）：
```yaml
patch:
  menu/page_size: 9
customization:
  generator: rime_patch
  modified_time: '...'
  distribution_code_name: ...
  rime_version: ...
```
而 `default.yaml` 的校验和不变。

**预期结果**：补丁文件被签名写入，官方文件原封不动——印证「物理分离」。若 `rime_patch` 不可用，可用 levers API 等价调用（见 4.1.4 的 C 路径），或标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`CustomSettings::Load` 之后调用 `GetValue("menu/page_size")`，拿到的是官方值还是补丁后的值？为什么？

**参考答案**：官方值。因为 `Load` 把官方配置读进 `config_`、补丁读进 `custom_config_`，二者不合并；`GetValue` 读的是 `config_`（见 [custom_settings.cc:61-63](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.cc#L61-L63)）。补丁要到部署期经 `config_builder` 编译才合并。

**练习 2**：`Customize` 为什么要把整个 `patch` 分支「整体 `SetItem` 回去」，而不是只调一次 `patch->Set(key, item)` 就完事？

**参考答案**：因为 `key` 可能是带斜杠的路径（如 `menu/page_size`），`ConfigMap::Set` 会把整个斜杠串当作单层字面键名；只有把整支 `patch` 重新挂回 `custom_config_`，保存时才能让编译器后续按路径正确解析（见 [custom_settings.cc:79-81](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/custom_settings.cc#L79-L81) 注释）。

### 4.3 SwitcherSettings：方案清单与热键

#### 4.3.1 概念说明

`SwitcherSettings` 继承 `CustomSettings`，固定 `config_id="default"`、`generator_id="Rime::SwitcherSettings"`，专门管理「方案切换器」相关的用户设置：磁盘上有哪些方案**可用**（`available_`）、用户**启用**了哪些（`selection_`）、切换器用什么**热键**（`hotkeys_`）。它把「选/不选某个方案」这种结构化操作，翻译成对 `default.custom.yaml` 里 `schema_list` 的 `patch`。

#### 4.3.2 核心流程

```
SwitcherSettings::Load():
   ├─ CustomSettings::Load()                              # 读 default 配置 + default.custom.yaml
   ├─ GetAvailableSchemasFromDirectory(shared_data_dir)   # 扫 *.schema.yaml
   ├─ GetAvailableSchemasFromDirectory(user_data_dir)     # 去重合并
   ├─ GetSelectedSchemasFromConfig()                      # 从 config_["schema_list"] 读已选
   └─ GetHotkeysFromConfig()                              # 从 config_["switcher/hotkeys"] 读热键

SwitcherSettings::Select(selection):
   ├─ 把 selection 包装成 [{schema: id}, ...] 的 ConfigList
   └─ Customize("schema_list", list)                      # 写进 default.custom.yaml 的 patch
```

#### 4.3.3 源码精读

[switcher_settings.h:15-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/switcher_settings.h#L15-L22) — `SchemaInfo` 结构：从 `*.schema.yaml` 的 `schema/` 段抽取的方案元信息（id / name / version / author / description / file_path）。
[switcher_settings.h:24-47](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/switcher_settings.h#L24-L47) — 类声明，定义 `SchemaList = vector<SchemaInfo>`、`Selection = vector<string>`（一组 schema_id）。

[switcher_settings.cc:18-19](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/switcher_settings.cc#L18-L19) — 构造即把 `config_id` 绑定为 `default`。

[switcher_settings.cc:49-94](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/switcher_settings.cc#L49-L94) — `GetAvailableSchemasFromDirectory`：遍历目录下 `*.schema.yaml`，要求必须有 `schema/schema_id` 与 `schema/name`（L61-64 缺一则 `continue` 跳过），再按 `schema_id` 去重（L66-74），最后补 version / author / description。shared 与 user 两个目录都扫，user 目录里的同名方案不会重复入列。

[switcher_settings.cc:96-112](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/switcher_settings.cc#L96-L112) — `GetSelectedSchemasFromConfig`：从 `config_["schema_list"]`（即 `default.yaml` 的 `schema_list`，见 [default.yaml:6-8](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L6-L8)）里取出每个 `schema: <id>`，拼成 `selection_`。

[switcher_settings.cc:33-42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/switcher_settings.cc#L33-L42) — `Select`：把一组 schema_id 重新构造成 `[{schema: id}, ...]` 的 `ConfigList`，交给基类 `Customize("schema_list", list)`。最终落到 `default.custom.yaml`（示例代码）：
```yaml
patch:
  schema_list:
    - schema: luna_pinyin
    - schema: stroke
```
下一次部署期 `WorkspaceUpdate` 读 `default.yaml` 的 `schema_list` 时，拿到的是合并后的清单（[deployment_tasks.cc:179-188](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L179-L188)）。

[switcher_settings.cc:114-131](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/switcher_settings.cc#L114-L131) — `GetHotkeysFromConfig`：把 `switcher/hotkeys` 列表（见 [default.yaml:10-15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L10-L15)）拼成逗号分隔字符串。`SetHotkeys` 则是 TODO、未实现（[switcher_settings.cc:44-47](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/switcher_settings.cc#L44-L47)，直接返回 false）。

C API：[levers_api_impl.h:92-95](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_api_impl.h#L92-L95) 构造、[levers_api_impl.h:97-134](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_api_impl.h#L97-L134) 读可用/已选清单、[levers_api_impl.h:171-180](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_api_impl.h#L171-L180) `select_schemas`。

#### 4.3.4 代码实践

**实践目标**：通过 levers API「只启用 luna_pinyin 与 stroke 两个方案」，验证它改的是 `default.custom.yaml` 而非 `default.yaml`。

**操作步骤**（源码阅读型 + 待本地验证）：

1. 读 [levers_api_impl.h:171-180](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_api_impl.h#L171-L180) 的 `select_schemas`，确认它把 C 层 `schema_id_list[]` 转成 `SwitcherSettings::Selection` 再调 `Select`。
2. 写一段最小 C 程序：`switcher_settings_init → load_settings → select_schemas({"luna_pinyin","stroke"}, 2) → save_settings`。
3. 查看 `user_data_dir/default.custom.yaml`。

**需要观察的现象**：`patch/schema_list` 被改写为只含这两个方案的列表。

**预期结果**：方案切换器（`F4` 或 `Control+grave`）下次唤起时只列出这两个方案。若无法编译运行，标注「待本地验证」，改为手工写等价的 `default.custom.yaml` 来验证。

#### 4.3.5 小练习与答案

**练习 1**：`available_` 与 `selection_` 的区别是什么？

**参考答案**：`available_` 是磁盘上「存在」的全部 `*.schema.yaml`（shared + user 目录扫描去重）；`selection_` 是 `default.yaml` 的 `schema_list` 里「启用」的子集。切换器运行期只展示 `selection_`（详见 u9-l4），而 `available_` 供前端做「添加方案」界面。

**练习 2**：`Select` 之后，运行中的引擎会立刻只剩这两个方案吗？

**参考答案**：不会。`Select` 只写了 `default.custom.yaml`；要等下一次部署（`WorkspaceUpdate` 重编译 `default.yaml`）把补丁合并后，再下次加载 `schema_list` 才生效。这正是 4.1 强调的「写补丁」与「合并补丁」分离。

### 4.4 UserDictManager：用户词典的备份与同步

#### 4.4.1 概念说明

`UserDictManager` 与前三节的「配置定制」是**平行**的用户数据管理职责，但它管的是**用户词典**（即 u8-l6 的 LevelDB `userdb`）：列举、备份成快照、从快照恢复、导出/导入文本、升级老格式、多设备同步。它完全不碰 YAML 配置，只操作 `user_data_dir` 下的 `.userdb` 库与 `sync_dir` 下的 `.userdb.txt` 快照。把它放在本讲，是因为它和 `CustomSettings` 同属 levers 模块「用户层管理」的一环，且同样由部署任务（`UserDictSync`、`UserDictUpgrade`）驱动。

#### 4.4.2 核心流程

```
GetUserDictList()     : 扫 user_data_dir，按后缀 (.userdb) 列举词典名
Backup(name)          : 打开只读库 → 校验 user_id → 导出 sync_dir/<name>.userdb.txt
Restore(snapshot)     : 临时库恢复快照 → 校验 IsUserDb → DbSource >> UserDbMerger 合并进目标库
Synchronize(name)     : 先合并 sync_dir 下各设备的快照 → 再 Backup 自己一份
SynchronizeAll()      : 对每个词典执行 Synchronize
Export/Import(name)   : TSV 文本导出/导入
UpgradeUserDict(name) : 老版 legacy_userdb → 备份到 trash → Restore 进新 userdb
```

#### 4.4.3 源码精读

[user_dict_manager.h:19-43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.h#L19-L43) — 类声明与公开接口。注意 [user_dict_manager.h:27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.h#L27) 的 CAVEAT：备份/恢复等操作前要先关闭正在使用的库。

[user_dict_manager.cc:23-28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.cc#L23-L28) — 构造时 `UserDb::Require("userdb")` 拿到默认后端组件（注册名 `userdb`，即 `LevelDb`，见 u8-l6），`path_ = user_data_dir`。

[user_dict_manager.cc:30-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.cc#L30-L49) — `GetUserDictList`：扫 `path_`，凡文件名以组件 `extension()`（`.userdb`）结尾者，去掉后缀即词典名。

[user_dict_manager.cc:51-71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.cc#L51-L71) — `Backup`：只读打开，若 `user_id` 不匹配则重建元数据（L55-61，跨设备恢复时常见），再把库导出到 `user_data_sync_dir()` 下 `<name>.userdb.txt` 快照。

[user_dict_manager.cc:73-105](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.cc#L73-L105) — `Restore`：先在一个 `.temp` 临时库里 `Restore` 快照、校验它是合法用户库（`IsUserDb`）并取出目标库名，再用 `DbSource >> UserDbMerger` 把数据合并进真正的目标库（L101-103）。`BOOST_SCOPE_EXIT` 保证临时库与目标库始终被正确关闭。

[user_dict_manager.cc:178-208](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.cc#L178-L208) — `Synchronize`：先遍历 `sync_dir` 的每个子目录，把同名快照 `Restore`（合并别人设备同步来的数据），**再** `Backup` 自己一份。这是「多设备同步」的核心——每台设备既从共享 sync 目录合并别人的数据，又把自己的最新状态导出回去。

[user_dict_manager.cc:154-176](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.cc#L154-L176) — `UpgradeUserDict`：检测老格式 `legacy_userdb`，备份到 `trash/` 后删除，再 `Restore` 进新库。

部署任务入口：[deployment_tasks.cc:522-525](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L522-L525) 的 `UserDictSync::Run → SynchronizeAll`；[deployment_tasks.cc:505-520](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L505-L520) 的 `UserDictUpgrade`。`rime_dict_manager` 工具（[rime_dict_manager.cc:56-113](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_dict_manager.cc#L56-L113)）把这些方法暴露成 `-l / -s / -b / -r / -e / -i` 命令行。

#### 4.4.4 代码实践

**实践目标**：用 `rime_dict_manager` 导出用户词典，查看学习数据的条目数。

**操作步骤**：

1. 先用 `rime_api_console` 输入并提交几个词，产生用户词典 `luna_pinyin.userdb`。
2. 运行 `rime_dict_manager --list`，确认词典名；运行 `rime_dict_manager --export luna_pinyin export.txt`。
3. 打开 `export.txt`。

**需要观察的现象**：`export.txt` 是 TSV，每行一条记录；终端打印 `exported N entries.`。

**预期结果**：能导出与用户学习数据对应的条目。若 `rime_dict_manager` 未编译，可阅读 [rime_dict_manager.cc:94-109](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/tools/rime_dict_manager.cc#L94-L109) 的导出/导入分支，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`Synchronize` 为什么「先 `Restore` 别人的快照、再 `Backup` 自己」，而不是反过来？

**参考答案**：先合并别人同步来的数据，再导出包含合并结果的最新快照，这样自己导出的快照是最全的；反过来则自己导出的快照会缺少刚合并进来的数据，下一轮同步又会重复合并，既冗余又可能产生不必要的冲突。

**练习 2**：`Backup` 里「user_id 不匹配就重建元数据」是为了解决什么场景？

**参考答案**：当用户从别的设备拷贝来一个 `.userdb`（或从 sync 快照恢复），库里记录的 user_id 与本机 `installation_id` 不一致；重建元数据把它「认领」为本机库，避免后续同步时身份混乱（可对照 u9-l2 的 `InstallationUpdate` 生成 `installation_id`）。

## 5. 综合实践

**任务**：为一个最小安装的 luna_pinyin 做三项用户定制/数据操作，并说明每一步分别由本讲的哪个对象 / 机制负责。

1. **改每页候选数 + 追加一个 filter**：手写 `luna_pinyin.custom.yaml`（示例代码，`@next` 是 u4-l3 的列表游标，表示追加到列表末尾）：
   ```yaml
   patch:
     "menu/page_size": 7
     "engine/filters/@next": charset_filter
   ```
   说明：这一步**只产生补丁文件**，对应 4.2 的 `CustomSettings` 维护职责；真正合并靠 4.1 的 `AutoPatchConfigPlugin` 在下次部署时完成。

2. **只启用 luna_pinyin 一个方案**：
   ```bash
   rime_patch default schema_list '[{schema: luna_pinyin}]'
   ```
   这等价于 4.3 的 `SwitcherSettings::Select({"luna_pinyin"})`。

3. **备份用户词典**：
   ```bash
   rime_dict_manager --sync
   ```
   对应 4.4 的 `UserDictManager::SynchronizeAll`。

**完成后验证**（触发一次重新部署：重启前端或调用 `start_maintenance`）：

- staging 区 `luna_pinyin.schema.yaml` 的 `menu/page_size` 已是 7，且 `engine/filters` 末尾多了 `charset_filter`；
- 切换器只剩 luna_pinyin；
- sync 目录下出现 `luna_pinyin.userdb.txt` 快照。

最后用一句话总结：步骤 1、2 是「**写补丁**」，由 `CustomSettings` / `SwitcherSettings` 负责；步骤 1、2 的「**合并补丁**」由 `AutoPatchConfigPlugin` + `ConfigFileUpdate` 在部署期完成；步骤 3 与补丁无关，属于「**用户数据**」管理。若本地无法运行全套，至少完成手写 `.custom.yaml` 并阅读 `AutoPatchConfigPlugin` 与 `ConfigFileUpdate` 源码确认合并链路，其余标注「待本地验证」。

## 6. 本讲小结

- 用户定制与官方配置**物理分离**：官方文件只读，所有修改落到 `user_data_dir` 的 `.custom.yaml` 补丁文件。
- 合并发生在**部署期编译**：`ConfigFileUpdate` 改用 `config_builder`，`AutoPatchConfigPlugin` 自动把 `.custom.yaml` 的 `patch:` 挂成 `__patch` 依赖，产物落 staging；运行期读到的就是合并后的配置。
- `CustomSettings` 只维护补丁文件（`config_` 官方 + `custom_config_` 补丁，**二者不合并**），提供 `Load / Customize / Save / IsFirstRun`；`GetValue` 查的是官方值。
- `Customizer` 是被取代的、就地改写官方文件本体的旧实现（已 DEPRECATED），理解它只为看清新机制的价值。
- `SwitcherSettings` 继承 `CustomSettings`，把「选方案」翻译成对 `default.custom.yaml` 的 `schema_list` patch；`SetHotkeys` 尚未实现。
- `UserDictManager` 与配置定制平行，管用户词典的备份 / 恢复 / 导出 / 同步 / 升级，由 `UserDictSync` / `UserDictUpgrade` 部署任务驱动。
- 这四个类都属 levers 模块，经 `RimeLeversApi` 暴露为 C API，`rime_patch` / `rime_dict_manager` 是其命令行入口。

## 7. 下一步学习建议

- 下一讲 **u9-l4「Switcher 与 Switches」** 会讲运行期的方案切换器（`Switcher` 引擎与 `switches` 配置模型），与本讲的 `SwitcherSettings`（部署期选方案）正好互补——一个管「有哪些方案可选」，一个管「运行期怎么切换与开关」。
- 想深入「合并」的内部细节，回头重读 **u4-l3**（`ConfigCompiler` 的 `__patch` 与依赖图）与 **u4-l4**（配置插件族），把 `AutoPatchConfigPlugin` 放进六个内置插件的全景。
- 用户词典同步的底层（`UserDbMerger` 三向合并、`user_db_recovery_task`）在 **u8-l6** 已展开，可对照阅读。
- 若想做带 UI 的配置工具，参考 `tools/rime_patch.cc` 与 `tools/rime_dict_manager.cc` 的 levers API 用法，它们是最小可运行样例。
