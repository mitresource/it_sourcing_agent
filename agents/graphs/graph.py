from langgraph.graph import StateGraph, END
import sys
sys.path.append("..")

from agents.nodes.state import RecruitmentState
from agents.nodes.fetch_jobs import fetch_pending_jobs_from_db
from agents.nodes.parse_jobs import prepare_job_data
from agents.nodes.evaluate_candidates_matches import evaluate_candidate_matches
from agents.nodes.save_results import save_results_to_files
from agents.nodes.send_email import send_summary_email


def has_more_jobs(state):
    """Check if there are more jobs to process."""
    index = state.get("current_job_index", 0)
    total = len(state.get("pending_jobs_id", []))
    if index < total:
        return "prepare_job_data"
    return "send_summary_email"


def build_recruitment_graph():
    workflow = StateGraph(RecruitmentState)

    workflow.add_node("fetch_pending_jobs_from_db", fetch_pending_jobs_from_db)
    workflow.add_node("prepare_job_data", prepare_job_data)
    workflow.add_node("evaluate_candidate_matches", evaluate_candidate_matches)
    workflow.add_node("save_results_to_files", save_results_to_files)
    workflow.add_node("send_summary_email", send_summary_email)

    workflow.set_entry_point("fetch_pending_jobs_from_db")
    workflow.add_edge("fetch_pending_jobs_from_db", "prepare_job_data")
    workflow.add_edge("prepare_job_data", "evaluate_candidate_matches")
    workflow.add_edge("evaluate_candidate_matches", "save_results_to_files")

    # Loop back to prepare_job_data if more jobs, otherwise send email and END
    workflow.add_conditional_edges("save_results_to_files", has_more_jobs)
    workflow.add_edge("send_summary_email", END)

    return workflow.compile()


recruitment_app = build_recruitment_graph()