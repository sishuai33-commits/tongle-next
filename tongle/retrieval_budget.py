"""One finite identity-verification budget per public read operation."""
from contextlib import contextmanager


class VerificationBudget:
    def __init__(self,maximum=256):
        self.maximum=maximum
        self.identities=set()
        self.exhausted=False

    def consume(self,kind,identity,version):
        key=(kind,identity,version)
        if key in self.identities:
            return True
        if len(self.identities)>=self.maximum:
            self.exhausted=True
            return False
        self.identities.add(key)
        return True

    def report(self):
        return {'direct_dependency_checks':len(self.identities),
            'direct_dependency_budget':self.maximum,
            'verification_budget_exhausted':self.exhausted,
            'checked_identity_counts':{kind:sum(key[0]==kind for key in self.identities)
                for kind in ('card','source')}}


@contextmanager
def verification_scope(engine):
    existing=getattr(engine,'_retrieval_verification_budget',None)
    if existing is not None:
        yield existing
        return
    budget=VerificationBudget()
    engine._retrieval_verification_budget=budget
    try:
        yield budget
    finally:
        del engine._retrieval_verification_budget


def verify_identity(engine,kind,identity,version):
    budget=getattr(engine,'_retrieval_verification_budget',None)
    return budget is None or budget.consume(kind,identity,version)


def verification_report(engine):
    budget=getattr(engine,'_retrieval_verification_budget',None)
    if budget is None:
        return {}
    report=budget.report()
    if budget.exhausted:
        report['degraded']='verification_budget_exceeded'
    return report
