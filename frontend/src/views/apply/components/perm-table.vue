<template>
    <div class="iam-apply-content">
        <render-vertical-block
            :label="applyTitle"
            ext-cls="apply-title">
            <bk-table
                :data="tableList"
                ext-cls="apply-content-table"
                border
                :cell-class-name="getCellClass">
                <bk-table-column :label="$t(`m.common['操作']`)">
                    <template slot-scope="{ row }">
                        <Icon
                            type="pin"
                            class="relate-action-tips-icon"
                            v-bk-tooltips="{ content: $t(`m.common['依赖操作']`), extCls: 'iam-tooltips-cls' }"
                            v-if="row.tag === 'related'" />
                        <span :title="row.name">{{ row.name }}</span>
                    </template>
                </bk-table-column>
                <bk-table-column :resizable="false" :label="$t(`m.common['资源实例']`)" width="491">
                    <template slot-scope="{ row }">
                        <template v-if="!row.isEmpty">
                            <p class="related-resource-item"
                                v-for="item in row.related_resource_types"
                                :key="item.type">
                                <render-resource-popover
                                    :key="item.type"
                                    :data="item.condition"
                                    :value="`${item.name}：${item.value}`"
                                    :max-width="380"
                                    @on-view="handleViewResource(row)" />
                            </p>
                        </template>
                        <template v-else>
                            {{ $t(`m.common['无需关联实例']`) }}
                        </template>
                        <Icon
                            type="detail-new"
                            class="view-icon"
                            :title="$t(`m.common['详情']`)"
                            v-if="!row.isEmpty"
                            @click.stop="handleViewResource(row)" />
                    </template>
                </bk-table-column>
                <bk-table-column prop="expired_dis" :label="$t(`m.common['申请期限']`)"></bk-table-column>
            </bk-table>
        </render-vertical-block>
        <bk-sideslider
            :is-show.sync="isShowSideslider"
            :title="sidesliderTitle"
            :width="725"
            :quick-close="true"
            @animation-end="handleAnimationEnd">
            <div slot="content">
                <component :is="renderDetailCom" :data="previewData" />
            </div>
        </bk-sideslider>
    </div>
</template>
<script>
    import _ from 'lodash'
    import Resource from '@/components/render-resource/detail'
    import RenderResourcePopover from '@/components/iam-view-resource-popover'
    import DetailContent from './detail-content'
    export default {
        name: '',
        components: {
            Resource,
            DetailContent,
            RenderResourcePopover
        },
        props: {
            data: {
                type: Array,
                default: () => []
            },
            system: {
                type: Object,
                default: () => {
                    return {}
                }
            },
            actionTopologies: {
                type: Array,
                default: () => []
            }
        },
        data () {
            return {
                previewData: {},
                renderDetailCom: 'DetailContent',
                isShowSideslider: false,
                sidesliderTitle: '',
                tableList: [],
                curId: ''
            }
        },
        computed: {
            applyTitle () {
                return `${this.$t(`m.myApply['申请内容']`)}（${this.system.system_name}）`
            }
        },
        watch: {
            data: {
                handler (value) {
                    this.tableList = _.cloneDeep(value)
                },
                immediate: true
            }
        },
        methods: {
            getCellClass ({ row, column, rowIndex, columnIndex }) {
                if (columnIndex === 1) {
                    return 'iam-perm-table-cell-cls'
                }
                return ''
            },

            handleViewResource (row) {
                this.previewData = _.cloneDeep(this.handleDetailData(row))
                this.renderDetailCom = 'DetailContent'
                this.sidesliderTitle = `${this.$t(`m.common['操作']`)}【${row.name}】${this.$t(`m.common['的资源实例']`)}`
                this.isShowSideslider = true
            },

            handleDetailData (payload) {
                this.curId = payload.id
                const params = []
                if (payload.related_resource_types.length > 0) {
                    payload.related_resource_types.forEach(item => {
                        const { name, type, condition } = item
                        params.push({
                            name: type,
                            label: `${name} ${this.$t(`m.common['实例']`)}`,
                            tabType: 'resource',
                            data: condition
                        })
                    })
                }
                return params
            }
        }
    }
</script>
<style lang='postcss'>
    .iam-apply-content {
        margin-top: 16px;
        padding: 20px 30px;
        background: #fff;
        border-radius: 2px;
        box-shadow: 0px 1px 2px 0px rgba(49, 50, 56, .1);
        .apply-title {
            .label {
                margin-bottom: 15px;
                font-size: 14px !important;
                color: #63656e;
                font-weight: bold;
            }
        }
        .bk-table-enable-row-hover .bk-table-body tr:hover > td {
            background-color: #fff;
        }
        .apply-content-table {
            border-right: none;
            border-bottom: none;
            .bk-table-header-wrapper {
                .cell {
                    padding-left: 20px !important;
                }
            }
            .relate-action-tips-icon {
                position: absolute;
                top: 50%;
                left: 5px;
                transform: translateY(-50%);
                &:hover {
                    color: #3a84ff;
                }
            }
            .bk-table-body-wrapper {
                .cell {
                    padding: 20px !important;
                    .view-icon {
                        display: none;
                        position: absolute;
                        top: 50%;
                        right: 10px;
                        transform: translate(0, -50%);
                        font-size: 18px;
                        cursor: pointer;
                    }
                    &:hover {
                        .view-icon {
                            display: inline-block;
                            color: #3a84ff;
                        }
                    }
                }
            }
            tr:hover {
                background-color: #fff;
            }
        }
    }
</style>
