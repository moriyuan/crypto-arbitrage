// Offline, idealized taker research. The frozen readers retain exact 002/003 scales.
// No latency, no simulated consumption of the historical book, no exchange APIs.
#define main frozen_cohort_main
#include "../20261012-btw-as-code/cohort_replay.cpp"
#undef main
#include <set>

static constexpr I NS_PER_MS=1000000;
static constexpr I COOLDOWN_NS=30000000000LL;
static constexpr I FUNDING_BUFFER_NS=60000000000LL;

struct IdealPolicy {std::string id;int entry=0,tp=0,sl=0,hard=0;I hold_ms=0;};
static std::vector<std::string> csv_fields(const std::string& line) {
    std::vector<std::string> result;std::stringstream in(line);std::string field;
    while(std::getline(in,field,','))result.push_back(field);
    if(line.empty() || line.back()==',')result.push_back("");
    return result;
}
static bool next_line(std::ifstream& in,std::string& line) {
    bool ok=bool(std::getline(in,line));if(!line.empty() && line.back()=='\r')line.pop_back();return ok;
}
static bool safe_id(const std::string& id) {
    if(id.empty() || id.size()>64)return false;
    auto alnum=[](char c){return (c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9');};
    if(!alnum(id[0]))return false;
    for(char c:id)if(!alnum(c) && c!='_' && c!='-')return false;
    return true;
}
static std::vector<IdealPolicy> read_policies(const std::string& path) {
    std::ifstream in(path);std::string line;
    if(!next_line(in,line) || line!="id,entry_bps,tp_bps,sl_bps,hard_bps,hold_ms")throw std::runtime_error("invalid policy CSV header");
    std::set<std::string> ids;std::vector<IdealPolicy> result;
    while(next_line(in,line)) {
        auto f=csv_fields(line);
        if(f.size()!=6 || !safe_id(f[0]) || !ids.insert(f[0]).second)throw std::runtime_error("invalid or duplicate policy id/row");
        std::array<I,5> n;for(int k=0;k<5;++k)n[k]=threshold_integer(f[k+1]);
        if(n[0]>10000 || n[1]<1 || n[1]>10000 || n[2]<1 || n[2]>10000 ||
           n[3]<1 || n[3]>10000 || n[4]<1 || n[4]>86400000)throw std::runtime_error("policy value outside supported range");
        result.push_back({f[0],int(n[0]),int(n[1]),int(n[2]),int(n[3]),n[4]});
        if(result.size()>1024)throw std::runtime_error("too many policies");
    }
    if(!in.eof() || result.empty())throw std::runtime_error("empty or unreadable policy CSV");
    return result;
}

struct FundingObservation {int venue;I known,next;};
class FundingSchedule {
    std::vector<FundingObservation> observations;size_t cursor=0;
public:
    std::array<I,2> next{};
    explicit FundingSchedule(const std::string& path) {
        std::ifstream in(path);std::string line;I previous=-1;std::array<I,2> venue_previous{-1,-1};
        if(!next_line(in,line) || line!="venue,known_at_ns,next_funding_ns")throw std::runtime_error("invalid funding CSV header");
        while(next_line(in,line)) {
            auto f=csv_fields(line);
            if(f.size()!=3 || (f[0]!="bybit" && f[0]!="bitget"))throw std::runtime_error("invalid funding CSV row");
            int v=f[0]=="bybit"?0:1;I known=threshold_integer(f[1]),funding=threshold_integer(f[2]);
            if(known<=0 || known<previous || known<=venue_previous[v] || funding<=known)throw std::runtime_error("noncausal or non-increasing funding observation");
            observations.push_back({v,known,funding});previous=known;venue_previous[v]=known;
        }
        if(!in.eof() || venue_previous[0]<0 || venue_previous[1]<0)throw std::runtime_error("funding CSV requires both venues");
    }
    void advance(I ns) {while(cursor<observations.size() && observations[cursor].known<=ns){auto& o=observations[cursor++];next[o.venue]=o.next;}}
};

// Validate observed levels once for all scenarios. No counterfactual liquidity debt.
class ObservedBook {
    std::array<std::map<I,I>,4> sides;
public:
    void observe(const Tick& t) {
        if(!t.valid)return;
        for(const auto& u:t.updates){if(!u.display)sides[u.side].erase(u.price);else sides[u.side][u.price]=u.display;}
        for(int side=0;side<4;++side) {
            if(sides[side].size()>50)throw std::runtime_error("more than 50 observed levels");
            auto it=sides[side].find(checked(Wide(t.price[side])*DEPTH_KEY_MULTIPLIER));
            if(it==sides[side].end() || it->second!=t.display[side])throw std::runtime_error("header L1 inconsistent with updates");
        }
        if(t.price[1]>t.price[0] || t.price[3]>t.price[2])throw std::runtime_error("crossed venue L1");
    }
};

// Open files only during bounded flushes: a 192-policy run must not need 576 descriptors.
class BufferedCsv {
    fs::path path;std::string buffer;
public:
    BufferedCsv(const fs::path& p,const std::string& header):path(p) {
        std::ofstream out(path);out<<header<<'\n';out.close();if(!out)throw std::runtime_error("cannot create CSV");
    }
    void row(const std::string& value){buffer+=value;buffer+='\n';if(buffer.size()>=65536)flush();}
    void flush(){if(buffer.empty())return;std::ofstream out(path,std::ios::app);out<<buffer;out.close();if(!out)throw std::runtime_error("CSV write failed");buffer.clear();}
};
struct IdealPosition {
    I id=0,entry_ns=0,qty=0,initial_net_per_coin=0,trigger_ns=0;
    int lng=0;std::array<I,2> entry{},entry_fees{};std::array<I,4> entry_quotes{};
    bool initial_hard_breach=false;std::string pending;
};

class IdealScenario {
public:
    IdealPolicy policy;I entry_start_ns;
    std::array<I,2> cash{INITIAL,INITIAL};std::unique_ptr<IdealPosition> pos;
    I ticks=0,valid_ticks=0,direction_evaluations=0,eligible=0,orders=0,cycles=0,wins=0,losses=0,flat=0;
    I total_gross=0,total_fees=0,closed_net=0,next_id=0,cooldown_ns=0;
    I marked_peak=2*INITIAL,marked_drawdown=0,complete_marks=0,incomplete_marks=0;
    I entry_gated_time=0,entry_gated_position=0,entry_gated_force=0,entry_gated_funding=0,entry_gated_halt=0,entry_gated_cooldown=0,entry_gated_activity=0;
    I activity_pass_decisions=0,activity_fail_decisions=0,entry_hard_breaches=0,depth_assumed_orders=0,below_min_exit_orders=0;
    I max_exit_excess_depth_qty=0,total_exit_excess_depth_qty=0,legacy_force1_ticks=0,funding_guard_decisions=0;
    bool day_ended=false;std::string halt;std::map<std::string,I> exits;
    std::unique_ptr<BufferedCsv> order_out,cycle_out,equity_out;
    explicit IdealScenario(IdealPolicy p,const fs::path& output,I start):policy(std::move(p)),entry_start_ns(start) {
        fs::path dir=output/policy.id;if(!fs::create_directory(dir))throw std::runtime_error("cannot create policy directory");
        order_out=std::make_unique<BufferedCsv>(dir/"orders.csv","ns,timestamp_ms,venue,side,intent,qty,price,fee,cash,cycle,exit_reason,l1_display_qty,excess_depth_qty,exit_below_min");
        cycle_out=std::make_unique<BufferedCsv>(dir/"cycles.csv","cycle,long_venue,short_venue,qty,entry_ns,exit_ns,entry_timestamp_ms,exit_timestamp_ms,holding_ms,long_entry_price,short_entry_price,gross_pnl,total_fees,net_before_funding,bybit_net_before_funding,bitget_net_before_funding,exit_reason,exit_parts,initial_quote_net_proxy,initial_hard_budget_breach,first_trigger_ns,trigger_remaining_net_proxy,holding_ns,long_exit_price,short_exit_price,entry_bybit_ask,entry_bybit_bid,entry_bitget_ask,entry_bitget_bid,exit_depth_assumed,exit_min_notional_assumed");
        equity_out=std::make_unique<BufferedCsv>(dir/"equity.csv","ns,event,cycle,bybit_cash,bitget_cash,total_cash,liquidation_equity,unresolved_qty");
    }
    bool funding_guard(I ns,const std::array<I,2>& next) const {
        I buffer=checked(Wide(policy.hold_ms)*NS_PER_MS+FUNDING_BUFFER_NS);
        return next[0]<=ns || next[1]<=ns || next[0]-ns<=buffer || next[1]-ns<=buffer;
    }
    Offer offer(const Tick& t,int lng) const {
        Offer o{lng,0,t.price[2*lng],t.price[2*(1-lng)+1]};
        I q=std::min(t.display[2*lng],t.display[2*(1-lng)+1]);
        for(int v=0;v<2;++v) {
            I px=t.price[v==lng?2*v:2*v+1];
            q=std::min(q,MAX_NOTIONAL_TICKS/px);
            q=std::min(q,checked(Wide(cash[v])/(Wide(px)*10003)));
        }
        q=floor10(q);if(q<=0 || Wide(q)*o.ask<MIN_NOTIONAL_TICKS || Wide(q)*o.bid<MIN_NOTIONAL_TICKS)return o;
        if(Wide(o.bid-o.ask)*20000<Wide(policy.entry)*(o.bid+o.ask))return o;
        o.qty=q;return o;
    }
    I net_per_coin(const Tick& t) const {
        const auto& p=*pos;I el=p.entry[p.lng],es=p.entry[1-p.lng],xl=t.price[2*p.lng+1],xs=t.price[2*(1-p.lng)];
        return checked(Wide(10000)*(Wide(xl)-el+es-xs)-Wide(3)*(Wide(el)+es+xl+xs));
    }
    I net_mark(const Tick& t) const{return checked(Wide(net_per_coin(t))*pos->qty);}
    I equity(const Tick& t) const {
        I value=checked(Wide(cash[0])+cash[1]);
        if(pos)value=checked(Wide(value)+net_mark(t)+pos->entry_fees[0]+pos->entry_fees[1]);
        return value;
    }
    void mark(const Tick& t) {
        if(pos && !t.valid){++incomplete_marks;return;}
        I value=equity(t);++complete_marks;marked_peak=std::max(marked_peak,value);marked_drawdown=std::max(marked_drawdown,checked(Wide(marked_peak)-value));
    }
    void equity_row(const Tick& t,const std::string& event,I cycle) {
        std::ostringstream out;out<<t.ns<<','<<event<<','<<cycle<<','<<dec(cash[0],MONEY_DECIMALS)<<','<<dec(cash[1],MONEY_DECIMALS)<<','<<dec(checked(Wide(cash[0])+cash[1]),MONEY_DECIMALS)<<',';
        if(!pos || t.valid)out<<dec(equity(t),MONEY_DECIMALS);
        out<<','<<(pos?pos->qty:0);equity_out->row(out.str());
    }
    void order(const Tick& t,int venue,const std::string& intent,I px,I fee_value,int side,const std::string& reason) {
        auto& p=*pos;bool buy=(venue==p.lng)==(intent=="entry");
        I excess=intent=="exit"?std::max<I>(0,p.qty-t.display[side]):0;
        bool below=intent=="exit" && Wide(px)*p.qty<MIN_NOTIONAL_TICKS;
        depth_assumed_orders+=excess>0;below_min_exit_orders+=below;max_exit_excess_depth_qty=std::max(max_exit_excess_depth_qty,excess);total_exit_excess_depth_qty=checked(Wide(total_exit_excess_depth_qty)+excess);
        std::ostringstream out;out<<t.ns<<','<<t.ms<<','<<VENUE[venue]<<','<<(buy?"buy":"sell")<<','<<intent<<','<<p.qty<<','<<dec(px,PRICE_DECIMALS)<<','<<dec(fee_value,MONEY_DECIMALS)<<','<<dec(cash[venue],MONEY_DECIMALS)<<','<<p.id<<','<<reason<<','<<t.display[side]<<','<<excess<<','<<below;
        order_out->row(out.str());++orders;
    }
    void enter(const Tick& t,const Offer& o) {
        pos=std::make_unique<IdealPosition>();auto& p=*pos;p.id=++next_id;p.entry_ns=t.ns;p.qty=o.qty;p.lng=o.lng;p.entry[o.lng]=o.ask;p.entry[1-o.lng]=o.bid;p.entry_quotes=t.price;
        p.initial_net_per_coin=net_per_coin(t);p.initial_hard_breach=Wide(p.initial_net_per_coin)*p.qty<=-Wide(p.entry[p.lng])*p.qty*policy.hard;entry_hard_breaches+=p.initial_hard_breach;
        for(int v=0;v<2;++v){I f=fee(p.entry[v],p.qty);p.entry_fees[v]=f;cash[v]=checked(Wide(cash[v])-f);total_fees=checked(Wide(total_fees)+f);order(t,v,"entry",p.entry[v],f,v==p.lng?2*v:2*v+1,"");}
        mark(t);equity_row(t,"entry",p.id);
    }
    void request(const Tick& t,const std::string& reason){pos->pending=reason;pos->trigger_ns=t.ns;}
    std::string risk(const Tick& t) const {
        const auto& p=*pos;I n=net_mark(t);Wide notional=Wide(p.entry[p.lng])*p.qty;
        if(Wide(n)<=-notional*policy.hard)return "hard_stop_loss";
        if(Wide(n)<=Wide(p.initial_net_per_coin)*p.qty-notional*policy.sl)return "stop_loss";
        if(Wide(n)>=notional*policy.tp)return "take_profit";
        return "";
    }
    void exit_position(const Tick& t) {
        auto& p=*pos;I trigger_net=net_mark(t);std::array<I,2> px{},gross{},fees{},net{};bool depth=false,below=false;
        for(int v=0;v<2;++v) {
            int side=v==p.lng?2*v+1:2*v;px[v]=t.price[side];depth|=p.qty>t.display[side];below|=Wide(px[v])*p.qty<MIN_NOTIONAL_TICKS;
            gross[v]=checked(Wide(p.qty)*(Wide(px[v])-p.entry[v])*10000*(v==p.lng?1:-1));
            I f=fee(px[v],p.qty);fees[v]=checked(Wide(p.entry_fees[v])+f);net[v]=checked(Wide(gross[v])-fees[v]);
            cash[v]=checked(Wide(cash[v])+gross[v]-f);total_gross=checked(Wide(total_gross)+gross[v]);total_fees=checked(Wide(total_fees)+f);
            order(t,v,"exit",px[v],f,side,p.pending);
        }
        I g=checked(Wide(gross[0])+gross[1]),f=checked(Wide(fees[0])+fees[1]),n=checked(Wide(g)-f),duration=t.ns-p.entry_ns;
        std::ostringstream out;out<<p.id<<','<<VENUE[p.lng]<<','<<VENUE[1-p.lng]<<','<<p.qty<<','<<p.entry_ns<<','<<t.ns<<','<<p.entry_ns/NS_PER_MS<<','<<t.ms<<','<<duration/NS_PER_MS<<','<<dec(p.entry[p.lng],PRICE_DECIMALS)<<','<<dec(p.entry[1-p.lng],PRICE_DECIMALS)<<','<<dec(g,MONEY_DECIMALS)<<','<<dec(f,MONEY_DECIMALS)<<','<<dec(n,MONEY_DECIMALS)<<','<<dec(net[0],MONEY_DECIMALS)<<','<<dec(net[1],MONEY_DECIMALS)<<','<<p.pending<<",1,"<<dec(checked(Wide(p.initial_net_per_coin)*p.qty),MONEY_DECIMALS)<<','<<p.initial_hard_breach<<','<<p.trigger_ns<<','<<dec(trigger_net,MONEY_DECIMALS)<<','<<duration<<','<<dec(px[p.lng],PRICE_DECIMALS)<<','<<dec(px[1-p.lng],PRICE_DECIMALS);
        for(I x:p.entry_quotes)out<<','<<dec(x,PRICE_DECIMALS);
        out<<','<<depth<<','<<below;cycle_out->row(out.str());
        ++cycles;wins+=n>0;losses+=n<0;flat+=n==0;closed_net=checked(Wide(closed_net)+n);++exits[p.pending];I cycle=p.id;
        cooldown_ns=checked(Wide(t.ns)+COOLDOWN_NS);pos.reset();
        if(checked(Wide(cash[0])+cash[1]-2*INITIAL)<=-INITIAL/100)halt="total_loss_cap";
        mark(t);equity_row(t,"exit",cycle);
    }
    void tick(const Tick& t,bool active,const std::array<I,2>& funding_next) {
        ++ticks;valid_ticks+=t.valid;legacy_force1_ticks+=t.force==1;activity_pass_decisions+=active;activity_fail_decisions+=!active;
        bool funding=funding_guard(t.ns,funding_next);funding_guard_decisions+=funding;
        if(t.force==2){day_ended=true;if(halt.empty())halt="replay_day_end";}
        // Force guards latch through missing books. Expiry is observed causally on ticks.
        if(pos) {
            if(t.force==2 && pos->pending!="replay_day_end")request(t,"replay_day_end");
            else if(funding && pos->pending!="replay_day_end" && pos->pending!="funding_guard")request(t,"funding_guard");
            else if(pos->pending.empty() && Wide(t.ns)-pos->entry_ns>=Wide(policy.hold_ms)*NS_PER_MS)request(t,"holding_timeout");
        }
        std::array<Offer,2> offers;
        if(t.valid && t.history)for(int v=0;v<2;++v){++direction_evaluations;offers[v]=offer(t,v);eligible+=offers[v].qty>0;}
        bool had=bool(pos);
        if(pos && t.valid) {
            if(pos->pending.empty()){auto r=risk(t);if(!r.empty())request(t,r);}
            if(!pos->pending.empty())exit_position(t);
        }
        Offer best;for(const auto& o:offers)if(o.qty && (!best.qty || Wide(o.bid-o.ask)*(best.bid+best.ask)>Wide(best.bid-best.ask)*(o.bid+o.ask)))best=o;
        if(best.qty) {
            entry_gated_position+=had;entry_gated_force+=day_ended;entry_gated_funding+=funding;entry_gated_halt+=!halt.empty();entry_gated_cooldown+=t.ns<cooldown_ns;entry_gated_time+=t.ns<entry_start_ns;entry_gated_activity+=!active;
        }
        if(!had && !day_ended && !funding && halt.empty() && t.ns>=cooldown_ns && t.ns>=entry_start_ns && active && best.qty) {
            enter(t,best);
            auto r=risk(t);if(!r.empty()){request(t,r);exit_position(t);}
        }
        mark(t);
    }
    void finish(const Tick& t){equity_row(t,"end",pos?pos->id:0);order_out->flush();cycle_out->flush();equity_out->flush();}
    void json(std::ostream& out) const {
        I cash_net=checked(Wide(cash[0])+cash[1]-2*INITIAL);
        out<<"{\"name\":\""<<policy.id<<"\",\"id\":\""<<policy.id<<"\",\"entry_bps\":"<<policy.entry<<",\"tp_bps\":"<<policy.tp<<",\"sl_bps\":"<<policy.sl<<",\"hard_cap_bps\":"<<policy.hard<<",\"hard_bps\":"<<policy.hard<<",\"hold_ms\":"<<policy.hold_ms<<",\"mode\":\"entry_deterioration\",\"activity_gate\":\"q25\",\"ticks\":"<<ticks<<",\"valid_ticks\":"<<valid_ticks<<",\"direction_evaluations\":"<<direction_evaluations<<",\"eligible_direction_evaluations\":"<<eligible<<",\"orders\":"<<orders<<",\"cycles\":"<<cycles<<",\"wins\":"<<wins<<",\"losses\":"<<losses<<",\"flat_cycles\":"<<flat<<",\"gross_pnl\":\""<<dec(total_gross,MONEY_DECIMALS)<<"\",\"fees\":\""<<dec(total_fees,MONEY_DECIMALS)<<"\",\"net_before_funding\":\""<<dec(cash_net,MONEY_DECIMALS)<<"\",\"closed_net_before_funding\":\""<<dec(closed_net,MONEY_DECIMALS)<<"\",\"cash\":{\"bybit\":\""<<dec(cash[0],MONEY_DECIMALS)<<"\",\"bitget\":\""<<dec(cash[1],MONEY_DECIMALS)<<"\"},\"unresolved_qty\":"<<(pos?pos->qty:0)<<",\"unresolved_cycle\":"<<(pos?pos->id:0)<<",\"unresolved_entry_ns\":"<<(pos?pos->entry_ns:0)<<",\"unresolved_long_venue\":\""<<(pos?VENUE[pos->lng]:"")<<"\",\"unresolved_exit_reason\":\""<<(pos?pos->pending:"")<<"\",\"halt_reason\":\""<<halt<<"\",\"day_end_observed\":"<<(day_ended?"true":"false")<<",\"initial_hard_budget_breaches\":"<<entry_hard_breaches<<",\"entry_gated_time\":"<<entry_gated_time<<",\"entry_gated_position\":"<<entry_gated_position<<",\"entry_gated_force\":"<<entry_gated_force<<",\"entry_gated_funding\":"<<entry_gated_funding<<",\"entry_gated_halt\":"<<entry_gated_halt<<",\"entry_gated_cooldown\":"<<entry_gated_cooldown<<",\"entry_gated_activity\":"<<entry_gated_activity<<",\"activity_pass_decisions\":"<<activity_pass_decisions<<",\"activity_fail_decisions\":"<<activity_fail_decisions<<",\"funding_guard_decisions\":"<<funding_guard_decisions<<",\"ignored_legacy_force1_ticks\":"<<legacy_force1_ticks<<",\"exit_depth_assumed_orders\":"<<depth_assumed_orders<<",\"exit_below_min_orders\":"<<below_min_exit_orders<<",\"total_exit_excess_depth_qty\":"<<total_exit_excess_depth_qty<<",\"max_exit_excess_depth_qty\":"<<max_exit_excess_depth_qty<<",\"complete_liquidation_equity_marks\":"<<complete_marks<<",\"incomplete_liquidation_equity_marks\":"<<incomplete_marks<<",\"complete_mark_max_drawdown_usdt\":\""<<dec(marked_drawdown,MONEY_DECIMALS)<<"\",\"exit_reasons\":{";
        bool first=true;for(const auto& r:exits){if(!first)out<<',';first=false;out<<'"'<<r.first<<"\":"<<r.second;}out<<"}}";
    }
};

int main(int argc,char** argv) {
    try {
        const std::set<std::string> required{"--tape","--activity","--thresholds","--output","--entry-start-ns","--policies","--funding-csv"};
        if(argc!=15)throw std::runtime_error("usage: perfect_taker --tape TAPE --activity ACT --thresholds CSV --output NEWDIR --entry-start-ns NS --policies CSV --funding-csv CSV");
        std::map<std::string,std::string> args;
        for(int k=1;k<argc;k+=2){std::string key=argv[k];if(!required.count(key) || args.count(key))throw std::runtime_error("unknown or duplicate CLI option");args[key]=argv[k+1];}
        I start=threshold_integer(args.at("--entry-start-ns"));if(start<=0)throw std::runtime_error("entry start must be positive");
        auto policies=read_policies(args.at("--policies"));auto gates=read_thresholds(args.at("--thresholds"));ActivityGate gate=gates.at(2);
        FundingSchedule funding(args.at("--funding-csv"));Tape tape(args.at("--tape"));ActivityTape activity(args.at("--activity"));
        fs::path output=args.at("--output");if(fs::exists(output))throw std::runtime_error("output already exists; refusing overwrite");
        if(!fs::create_directories(output))throw std::runtime_error("cannot create output directory");
        std::vector<std::unique_ptr<IdealScenario>> scenarios;for(const auto& p:policies)scenarios.push_back(std::make_unique<IdealScenario>(p,output,start));
        Tick t,last;Activity a;ObservedBook book;I count=0,first_ns=0;
        while(tape.next(t)) {
            if(!activity.next(a) || a.ns!=t.ns)throw std::runtime_error("activity/tape timestamp or length mismatch");
            book.observe(t);funding.advance(t.ns);bool active=gate.failures(a).empty();
            if(!count)first_ns=t.ns;last=t;
            for(auto& s:scenarios)s->tick(t,active,funding.next);
            if(++count%250000==0)std::cerr<<"processed "<<count<<" decisions; policies="<<scenarios.size()<<"\n";
        }
        if(activity.next(a))throw std::runtime_error("activity has extra rows");if(!count)throw std::runtime_error("empty tape");
        for(auto& s:scenarios)s->finish(last);
        std::ofstream out(output/"aggregate.json");
        out<<"{\"status\":\"COMPLETED_IDEALIZED_REPLAY_REQUIRES_FUNDING_AUDIT\",\"version\":1,\"execution_enabled\":false,\"execution_model\":\"zero_latency_synchronous_full_L1_taker\",\"entry_depth_fraction\":1,\"historical_depth_consumed\":false,\"exit_depth_and_min_notional_waived\":true,\"fee_rate\":\"0.0003\",\"initial_cash_per_venue\":\"1000\",\"max_notional_per_leg\":\"250\",\"leverage\":1,\"common_quantity_step\":10,\"entry_min_notional\":\"5\",\"cooldown_ns\":"<<COOLDOWN_NS<<",\"total_loss_cap_usdt\":\"10\",\"entry_start_ns\":"<<start<<",\"funding_buffer_ns\":"<<FUNDING_BUFFER_NS<<",\"funding_guard\":\"causal latest observation per venue; unknown/expired/remaining <= policy hold + 60 seconds\",\"legacy_force1_ignored\":true,\"tape_format\":\""<<TAPE_FORMAT<<"\",\"price_decimals\":"<<PRICE_DECIMALS<<",\"money_decimals\":"<<MONEY_DECIMALS<<",\"native_tick_units\":["<<NATIVE_TICK_UNITS[0]<<','<<NATIVE_TICK_UNITS[1]<<"],\"activity_format\":\"BTWACT01\",\"activity_scope\":\"entry_only_q25\",\"activity_thresholds\":{\"countA\":"<<gate.count[0]<<",\"countB\":"<<gate.count[1]<<",\"turnoverAunits\":"<<gate.turnover[0]<<",\"turnoverBunits\":"<<gate.turnover[1]<<",\"maxAgeNs\":"<<gate.max_age<<"},\"decisions\":"<<count<<",\"first_ns\":"<<first_ns<<",\"last_ns\":"<<last.ns<<",\"initial_exit_baseline\":\"entry_immediate_reverse_L1_including_four_fees\",\"drawdown_caveat\":\"ideal full L1 liquidation each valid decision; invalid open-position marks omitted\",\"equity_csv_sampling\":\"entry_exit_and_end_only; drawdown calculated on every valid decision\",\"net_before_funding_scope\":\"realized gross less all paid fees, including unresolved entry fees; excludes funding and unresolved unrealized PnL\",\"scenarios\":[";
        for(size_t i=0;i<scenarios.size();++i){if(i)out<<',';scenarios[i]->json(out);}out<<"]}\n";out.close();if(!out)throw std::runtime_error("aggregate write failed");
        std::cout<<"ideal replay completed: "<<count<<" decisions; "<<scenarios.size()<<" policies\n";return 0;
    }catch(const std::exception& e){std::cerr<<"ERROR: "<<e.what()<<'\n';return 1;}
}
