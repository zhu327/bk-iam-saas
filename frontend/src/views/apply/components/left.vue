<template>
    <div class="iam-left-wrapper">
        <filter-item
            :title="title"
            :data="filterData"
            :active="active"
            @on-change="handleFilterChange" />
        <div
            :class="['apply-wrapper', { 'set-right-border': isEmpty || isLoading }]"
            v-bkloading="{ isLoading, opacity: 1, color: '#fff' }"
            @scroll="handleScroll">
            <template v-if="!isEmpty">
                <apply-item
                    v-for="(item, index) in data"
                    :key="item.id"
                    :data="item"
                    :has-bottom-border="index !== data.length - 1"
                    :active="currentActive"
                    @on-change="handleChange"></apply-item>
                <div class="load-more-wrapper"
                    v-bkloading="{ isLoading: isScrollLoading, opacity: 1, color: '#fff' }"
                    v-if="isScrollLoading"></div>
                <div class="no-data-tips" v-show="isShowNoDataTips">{{ $t(`m.common['没有更多内容了']`) }}</div>
            </template>
            <template v-else>
                <div class="empty-wrapper">
                    <iam-svg />
                    <div class="empty-tip">{{ $t(`m.common['暂无数据']`) }}</div>
                </div>
            </template>
        </div>
    </div>
</template>
<script>
    import il8n from '@/language'
    import FilterItem from './filter-item'
    import ApplyItem from './apply-item'

    export default {
        name: '',
        components: {
            FilterItem,
            ApplyItem
        },
        props: {
            data: {
                type: Array,
                default: () => []
            },
            title: {
                type: String,
                default: il8n('myApply', '申请列表')
            },
            filterData: {
                type: Object,
                default: () => {
                    return {
                        'all': il8n('common', '全部'),
                        'wait': il8n('myApproval', '待审批')
                    }
                }
            },
            active: {
                type: String,
                default: 'wait'
            },
            isLoading: {
                type: Boolean,
                default: false
            },
            canScrollLoad: {
                type: Boolean,
                default: false
            }
        },
        data () {
            return {
                currentActive: '',
                isScrollLoading: false,
                isShowNoDataTips: false
            }
        },
        computed: {
            isEmpty () {
                return this.data.length < 1 && !this.isLoading
            }
        },
        watch: {
            data: {
                handler (value) {
                    if (value.length) {
                        if (!value.some(item => item.id === this.currentActive)) {
                            this.currentActive = value[0].id
                        }
                    } else {
                        this.currentActive = ''
                        this.isShowNoDataTips = false
                    }
                },
                immediate: true
            },
            isLoading () {
                this.isShowNoDataTips = false
            }
        },
        methods: {
            handleChange (payload) {
                this.currentActive = payload.id
                this.$emit('on-change', payload)
            },
            handleFilterChange (payload) {
                this.handleResetScrollLoading()
                this.$emit('on-filter-change', payload)
            },
            handleResetScrollLoading () {
                this.isShowNoDataTips = false
                this.isScrollLoading = false
            },
            handleScroll (event) {
                if (this.isLoading) {
                    this.handleResetScrollLoading()
                    return
                }
                if (!this.canScrollLoad) {
                    this.isShowNoDataTips = true
                    this.isScrollLoading = false
                    return
                }
                if (event.target.scrollTop + event.target.offsetHeight >= event.target.scrollHeight) {
                    this.isScrollLoading = true
                    this.isShowNoDataTips = false
                    this.$emit('on-load')
                }
            }
        }
    }
</script>
<style lang='postcss' scoped>
    .iam-left-wrapper {
        height: 100%;
        .filter-item {
            height: 42px;
            border-right: 1px solid #dcdee5;
            border-bottom: 1px solid #dcdee5;
        }
        .apply-wrapper {
            position: relative;
            height: calc(100% - 43px);
            overflow-x: hidden;
            overflow-y: auto;
            &.set-right-border {
                border-right: 1px solid #dcdee5;
            }
            &::-webkit-scrollbar {
                display: none;
                width: 4px;
                background-color: lighten(transparent, 80%);
            }
            &::-webkit-scrollbar-thumb {
                display: none;
                height: 5px;
                border-radius: 2px;
                background-color: #e6e9ea;
            }
            .empty-wrapper {
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                img {
                    width: 120px;
                }
            .empty-tip {
                position: relative;
                top: -25px;
                font-size: 12px;
                color: #c4c6cc;
                text-align: center;
                }
            }
            .load-more-wrapper {
                height: 70px;
                border-right: 1px solid #dcdee5;
            }
            .no-data-tips {
                padding: 10px 0;
                font-size: 12px;
                color: #979ba5;
                border-right: 1px solid #dcdee5;
                text-align: center;
            }
        }
    }
</style>
