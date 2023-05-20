"""
Module to generate video summaries with topics.
"""
import time
import typing
from typing import List, Dict
import db
from models import Video, Segment

import numpy as np
from scipy.spatial.distance import cosine

from langchain.embeddings import OpenAIEmbeddings
from langchain import OpenAI, PromptTemplate, LLMChain
from langchain.docstore.document import Document
from langchain.chains.summarize import load_summarize_chain
from langchain.chat_models import ChatOpenAI

import networkx as nx
from networkx.algorithms import community


def create_sentences(segments, MIN_WORDS, MAX_WORDS):
    # Combine the non-sentences together
    sentences = []

    is_new_sentence = True
    sentence_length = 0
    sentence_num = 0
    sentence_segments = []

    for i in range(len(segments)):
        if is_new_sentence:
            is_new_sentence = False

        # Append the segment
        sentence_segments.append(segments[i].strip().replace("  ", " "))
        segment_words = segments[i].split(" ")
        sentence_length += len(segment_words)

        # If exceed MAX_WORDS, then stop at the end of the segment
        # Only consider it a sentence if the length is at least MIN_WORDS
        if (
            sentence_length >= MIN_WORDS and segments[i][-1] == "."
        ) or sentence_length >= MAX_WORDS:
            sentence = " ".join(sentence_segments)
            sentences.append(
                {
                    "sentence_num": sentence_num,
                    "text": sentence,
                    "sentence_length": sentence_length,
                }
            )
            # Reset
            is_new_sentence = True
            sentence_length = 0
            sentence_segments = []
            sentence_num += 1

    return sentences


def create_chunks(sentences: typing.List, CHUNK_LENGTH: int, STRIDE: int):
    chunks = []
    for i in range(0, len(sentences), (CHUNK_LENGTH - STRIDE)):
        chunk = sentences[i : i + CHUNK_LENGTH]
        chunk_text = " ".join(c["text"] for c in chunk)
        chunks.append(
            {
                "start_sentence_num": chunk[0]["sentence_num"],
                "end_sentence_num": chunk[-1]["sentence_num"],
                "text": chunk_text,
                "num_words": len(chunk_text.split(" ")),
            }
        )
    return chunks


def create_chunks_from_segments(
    segments: List[Segment], CHUNK_LENGTH: int, STRIDE: int
) -> List[Dict]:
    chunks = []
    for i in range(0, len(segments), (CHUNK_LENGTH - STRIDE)):
        chunk = segments[i : i + CHUNK_LENGTH]
        chunk_text = " ".join(c.text for c in chunk)
        chunks.append(
            {
                "start_segment": chunk[0].id,
                "end_segment": chunk[-1].id,
                "text": chunk_text,
                "num_words": len(chunk_text.split(" ")),
            }
        )
    return chunks


def parse_title_summary_results(results):
    out = []
    for e in results:
        e = e.replace("\n", "")
        if "|" in e:
            processed = {"title": e.split("|")[0], "summary": e.split("|")[1][1:]}
        elif ":" in e:
            processed = {"title": e.split(":")[0], "summary": e.split(":")[1][1:]}
        elif "-" in e:
            processed = {"title": e.split("-")[0], "summary": e.split("-")[1][1:]}
        else:
            processed = {"title": "", "summary": e}
        out.append(processed)
    return out


def summarize_chunks(
    chunks_text, model_name="text-davinci-003"
) -> List[Dict[str, str]]:
    # set the model class to instantiate
    if model_name == "text-davinci-003":
        llm_model = OpenAI
    elif model_name == "gpt-3.5-turbo":
        llm_model = ChatOpenAI
    else:
        raise Exception(f"Model {model_name} not supported.")

    start_time = time.time()

    # Prompt to get title and summary for each chunk
    map_prompt_template = """Firstly, give the following text an informative title. Then, on a new line, write a 75-100 word summary of the following text:
    {text}

    Return your answer in the following format:
    Title | Summary...
    e.g. 
    God is love | The God of the Bible is a God of love and justice shown through the Cross of Christ.

    TITLE AND CONCISE SUMMARY:
    """

    map_prompt = PromptTemplate(template=map_prompt_template, input_variables=["text"])

    # Define the LLMs
    map_llm = llm_model(temperature=0, model_name=model_name)
    map_llm_chain = LLMChain(llm=map_llm, prompt=map_prompt)
    map_llm_chain_input = [{"text": t} for t in chunks_text]

    # Run the input through the LLM chain (works in parallel)
    map_llm_chain_results = map_llm_chain.apply(map_llm_chain_input)

    output = parse_title_summary_results([e["text"] for e in map_llm_chain_results])

    print(f"Stage 1 done time {time.time() - start_time}")

    return output


def get_embeddings(summaries):
    # Use OpenAI to embed the titles. Size of _embeds: (num_chunks x 1536)
    openai_embed = OpenAIEmbeddings()

    summary_embeds = np.array(openai_embed.embed_documents(summaries))

    num_chunks = len(summaries)
    print(f"Number of chunks: {num_chunks}")

    # Get similarity matrix between the embeddings of the chunk summaries
    summary_similarity_matrix = np.zeros((num_chunks, num_chunks))
    summary_similarity_matrix[:] = np.nan

    for row in range(num_chunks):
        for col in range(row, num_chunks):
            similarity = 1 - cosine(summary_embeds[row], summary_embeds[col])
            summary_similarity_matrix[row, col] = similarity
            summary_similarity_matrix[col, row] = similarity
    return summary_similarity_matrix


def get_louvain_communities(
    summary_similarity_matrix: np.ndarray,
    num_topics=8,
    bonus_constant=0.15,
    min_size=3,
    resolution=0.85,
    resolution_step=0.01,
    iterations=40,
) -> typing.Tuple[List[int], List[List[int]]]:
    """
    summary_similarity_matrix is a (n x n) matrix where n is the number of
    chunks generated.

    Returns two elements. First is a list, chunk_topics where
    chunk_topics[i] is the topic_id that the i'th chunk belongs to.
    topics_title[j] contains the list of chunk indexes that belong
    to the j'th topic.
    """
    proximity_bonus_arr = np.zeros_like(summary_similarity_matrix)
    for row in range(proximity_bonus_arr.shape[0]):
        for col in range(proximity_bonus_arr.shape[1]):
            proximity_bonus_arr[row, col] = (
                0 if row == col else 1 / (abs(row - col)) * bonus_constant
            )

    summary_similarity_matrix += proximity_bonus_arr

    title_nx_graph = nx.from_numpy_array(summary_similarity_matrix)
    desired_num_topics = num_topics

    # Store the accepted partitionings
    topics_title_accepted = []

    # Find the resolution that gives the desired number of topics
    topics_title = []
    while len(topics_title) not in [
        desired_num_topics,
        desired_num_topics + 1,
        desired_num_topics + 2,
    ]:
        topics_title = community.louvain_communities(
            title_nx_graph, weight="weight", resolution=resolution
        )
        resolution += resolution_step
    topic_sizes = [len(c) for c in topics_title]
    sizes_sd = np.std(topic_sizes)

    print(f"Using resolution {resolution}")

    lowest_sd_iteration = 0
    lowest_sd = float("inf")

    highest_mod_iteration = 0
    highest_mod = float("-inf")

    for i in range(iterations):
        topics_title = community.louvain_communities(
            title_nx_graph, weight="weight", resolution=resolution
        )
        modularity = community.modularity(
            title_nx_graph, topics_title, weight="weight", resolution=resolution
        )

        # Check SD
        topic_sizes = [len(c) for c in topics_title]
        sizes_sd = np.std(topic_sizes)

        topics_title_accepted.append(topics_title)

        if sizes_sd < lowest_sd and min(topic_sizes) >= min_size:
            lowest_sd_iteration = i
            lowest_sd = sizes_sd

        if modularity > highest_mod:
            highest_mod = modularity
            highest_mod_iteration = i

    # Set the chosen partitioning to be the one with highest modularity
    topics_title = topics_title_accepted[highest_mod_iteration]
    print(f"Best SD: {lowest_sd}, Best iteration: {lowest_sd_iteration}")
    print(f"Best modularity: {highest_mod}, Best iteration: {highest_mod_iteration}")

    # Arrange title_topics in order of topic_id_means
    topic_id_means = [sum(e) / len(e) for e in topics_title]
    topics_title = [
        list(c)
        for _, c in sorted(zip(topic_id_means, topics_title), key=lambda pair: pair[0])
    ]

    # Create an array denoting which topic each chunk belongs to
    chunk_topics = [None] * summary_similarity_matrix.shape[0]
    for i, c in enumerate(topics_title):
        for j in c:
            chunk_topics[j] = i

    return chunk_topics, topics_title


def summarize_by_topics(
    chunk_summaries: List[Dict[str, str]],
    chunk_topic_groups: List[List[int]],
    summary_num_words=250,
    model_name="text-davinci-003",
):
    # set the model class to instantiate
    model_kwargs = {"temperature": 0, "model_name": model_name}
    if model_name == "text-davinci-003":
        llm_model = OpenAI
    elif model_name == "gpt-3.5-turbo":
        llm_model = ChatOpenAI
    else:
        raise Exception(f"Model {model_name} not supported.")

    start_time = time.time()

    # Prompt that passes in all the titles of a topic, and asks for an overall title of the topic
    title_prompt_template = """Write an informative title that summarizes each of the following groups of titles. Make sure that the titles capture as much information as possible, 
    and are different from each other:
    {text}

    Return your answer in a numbered list, with new line separating each title: 
    1. Title 1
    2. Title 2
    3. Title 3

    TITLES:
    """

    map_prompt_template = """Write a 75-100 word summary of the following text. It is a condensed transcription of a sermon preached by Dr. Martyn Lloyd-Jones.
    {text}

    CONCISE SUMMARY:"""

    combine_prompt_template = (
        "Write a "
        + str(summary_num_words)
        + """-word summary of the following sermon preached by Martyn Lloyd-Jones, removing irrelevant information. Finish your answer:
    {text}
    """
        + str(summary_num_words)
        + """-WORD SUMMARY:"""
    )

    title_prompt = PromptTemplate(
        template=title_prompt_template, input_variables=["text"]
    )
    map_prompt = PromptTemplate(template=map_prompt_template, input_variables=["text"])
    combine_prompt = PromptTemplate(
        template=combine_prompt_template, input_variables=["text"]
    )

    # Groups all the summaries and titles of chunks belonging to a topic together
    topics_data = []
    for c in chunk_topic_groups:
        topic_data = {
            "summaries": [chunk_summaries[chunk_id]["summary"] for chunk_id in c],
            "titles": [chunk_summaries[chunk_id]["title"] for chunk_id in c],
        }
        topic_data["summaries_concat"] = " ".join(topic_data["summaries"])
        topic_data["titles_concat"] = ", ".join(topic_data["titles"])
        topics_data.append(topic_data)

    # Get a list of each community's summaries (concatenated)
    topics_summary_concat = [c["summaries_concat"] for c in topics_data]
    topics_titles_concat = [c["titles_concat"] for c in topics_data]

    # Concat into one long string to do the topic title creation
    topics_titles_concat_all = """"""
    for i, c in enumerate(topics_titles_concat):
        topics_titles_concat_all += f"""{i+1}. {c}
        """

    title_llm = llm_model(**model_kwargs)
    title_llm_chain = LLMChain(llm=title_llm, prompt=title_prompt)
    title_llm_chain_input = [{"text": topics_titles_concat_all}]
    title_llm_chain_results = title_llm_chain.apply(title_llm_chain_input)

    # Split by new line
    titles = title_llm_chain_results[0]["text"].split("\n")
    # Remove any empty titles
    titles = [t for t in titles if t != ""]
    # Remove spaces at start or end of each title
    titles = [t.strip() for t in titles]

    map_llm = llm_model(**model_kwargs)
    reduce_llm = (
        llm_model(**model_kwargs, max_tokens=-1)
        if llm_model == OpenAI
        else llm_model(**model_kwargs)
    )

    # Run the map-reduce chain
    docs = [Document(page_content=t) for t in topics_summary_concat]
    chain = load_summarize_chain(
        chain_type="map_reduce",
        map_prompt=map_prompt,
        combine_prompt=combine_prompt,
        return_intermediate_steps=True,
        llm=map_llm,
        reduce_llm=reduce_llm,
    )

    output = chain({"input_documents": docs}, return_only_outputs=True)
    summaries = output["intermediate_steps"]
    topic_outputs = [{"title": t, "summary": s} for t, s in zip(titles, summaries)]
    final_summary = output["output_text"]

    print(f"Stage 2 done time {time.time() - start_time}")

    return topic_outputs, final_summary


def generate_summary(video: typing.Union[str, Video], model_name="text-davinci-003"):
    # fetch video
    if isinstance(video, str):
        video = db.get_video(video, with_segment=True, columns="transcription")

    # preprocess transcription, split into sentences
    for i, s in enumerate(video.segments):
        video.segments[i].text = s.text.strip().replace("  ", " ")

    # combine sentences into chunks (4:1 ratio)
    chunks = create_chunks_from_segments(video.segments, CHUNK_LENGTH=4, STRIDE=1)
    chunks_text = [chunk["text"].strip() for chunk in chunks]

    # use LLM to generate titles and summaries of chunks
    chunk_summaries = summarize_chunks(chunks_text)

    output_summaries = [e["summary"] for e in chunk_summaries]
    output_titles = [e["title"] for e in chunk_summaries]

    # generate embedding vectors of titles and summaries
    # TODO: save embeddings of chunk summaries (?)
    summary_similarity = get_embeddings(output_summaries)

    # use louvain communities to generate topics
    chunk_topics, topic_groups = get_louvain_communities(summary_similarity)

    # use LLM to generate final titles and summaries of each topic
    topic_outputs, final_summary = summarize_by_topics(
        chunk_summaries, topic_groups, model_name=model_name
    )

    return topic_outputs, final_summary


# TODO: save to db topic to segment link: topic -> chunk -> segment
# TODO: save to db topics and summaries

if __name__ == "__main__":
    topics, summary = generate_summary("K3AwnWcvtzQ", model_name="gpt-3.5-turbo")